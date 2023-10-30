#!/usr/bin/env python3
# Copyright 2023 Canonical
# See LICENSE file for licensing details.

"""Charm."""

import logging
import os
import socket

import auth_webhook
import charms.contextual_status as status
import leader_data
import ops
import yaml
from cdk_addons import CdkAddons
from charms import kubernetes_snaps
from charms.interface_container_runtime import ContainerRuntimeProvides
from charms.interface_external_cloud_provider import ExternalCloudProvider
from charms.interface_kube_dns import KubeDnsRequires
from charms.interface_kubernetes_cni import KubernetesCniProvides
from charms.kubernetes_libs.v0.etcd import EtcdReactiveRequires
from charms.reconciler import Reconciler
from k8s_api_endpoints import K8sApiEndpoints
from loadbalancer_interface import LBProvider
from ops import BlockedStatus, WaitingStatus
from ops.interface_kube_control import KubeControlProvides
from ops.interface_tls_certificates import CertificatesRequires

log = logging.getLogger(__name__)


class KubernetesControlPlaneCharm(ops.CharmBase):
    """Charm."""

    def __init__(self, *args):
        super().__init__(*args)
        self.cdk_addons = CdkAddons(self)
        self.certificates = CertificatesRequires(self, endpoint="certificates")
        self.cni = KubernetesCniProvides(
            self, endpoint="cni", default_cni=self.model.config["default-cni"]
        )
        self.container_runtime = ContainerRuntimeProvides(self, endpoint="container-runtime")
        self.etcd = EtcdReactiveRequires(self)
        self.k8s_api_endpoints = K8sApiEndpoints(self)
        self.kube_control = KubeControlProvides(self, endpoint="kube-control")
        self.kube_dns = KubeDnsRequires(self, endpoint="dns-provider")
        self.lb_external = LBProvider(self, "loadbalancer-external")
        self.lb_internal = LBProvider(self, "loadbalancer-internal")
        self.external_cloud_provider = ExternalCloudProvider(self, "external-cloud-provider")
        self.reconciler = Reconciler(self, self.reconcile)

    def api_dependencies_ready(self):
        common_name = kubernetes_snaps.get_public_address()
        ca = self.certificates.ca
        client_cert = self.certificates.client_certs_map.get("system:kube-apiserver")
        server_cert = self.certificates.server_certs_map.get(common_name)

        if not ca or not client_cert or not server_cert:
            status.add(WaitingStatus("Waiting for certificates"))
            return False

        if not self.etcd.is_ready:
            status.add(WaitingStatus("Waiting for etcd"))
            return False

        return True

    def configure_apiserver(self):
        kubernetes_snaps.configure_apiserver(
            advertise_address=self.kube_control.ingress_addresses[0],
            audit_policy=self.model.config["audit-policy"],
            audit_webhook_conf=self.model.config["audit-webhook-config"],
            auth_webhook_conf=auth_webhook.auth_webhook_conf,
            authorization_mode=self.model.config["authorization-mode"],
            cluster_cidr=self.cni.cidr,
            etcd_connection_string=self.etcd.get_connection_string(),
            extra_args_config=self.model.config["api-extra-args"],
            privileged=self.model.config["allow-privileged"],
            service_cidr=self.model.config["service-cidr"],
            external_cloud_provider=self.external_cloud_provider,
        )

    def configure_auth_webhook(self):
        auth_webhook.configure(
            charm_dir=self.charm_dir,
            custom_authn_endpoint=self.model.config["authn-webhook-endpoint"],
            # TODO: aws iam, keystone
            # aws_iam_endpoint=???,
            # keystone_endpoint=???
        )

    def configure_container_runtime(self):
        if not self.container_runtime.relations:
            status.add(BlockedStatus("Missing container-runtime integration"))
            return

        registry = self.model.config["image-registry"]
        sandbox_image = kubernetes_snaps.get_sandbox_image(registry)
        self.container_runtime.set_sandbox_image(sandbox_image)

    def configure_cni(self):
        self.cni.set_image_registry(self.model.config["image-registry"])
        self.cni.set_kubeconfig_hash_from_file("/root/.kube/config")
        self.cni.set_service_cidr(self.model.config["service-cidr"])
        kubernetes_snaps.set_default_cni_conf_file(self.cni.cni_conf_file)

    def configure_controller_manager(self):
        cluster_name = self.get_cluster_name()
        if not cluster_name:
            status.add(WaitingStatus("Waiting for cluster name"))
            return

        kubernetes_snaps.configure_controller_manager(
            cluster_cidr=self.cni.cidr,
            cluster_name=cluster_name,
            extra_args_config=self.model.config["controller-manager-extra-args"],
            kubeconfig="/root/cdk/kubecontrollermanagerconfig",
            service_cidr=self.model.config["service-cidr"],
            external_cloud_provider=self.external_cloud_provider,
        )

    def configure_kernel_parameters(self):
        sysctl = yaml.safe_load(self.model.config["sysctl"])
        kubernetes_snaps.configure_kernel_parameters(sysctl)

    def configure_kube_control(self):
        dns_address = self.get_dns_address()
        dns_domain = self.get_dns_domain()
        dns_enabled = bool(dns_address)
        dns_port = self.get_dns_port()

        self.kube_control.set_api_endpoints([self.k8s_api_endpoints.internal()])
        self.kube_control.set_cluster_name(self.get_cluster_name())
        self.kube_control.set_default_cni(self.model.config["default-cni"])
        self.kube_control.set_dns_address(dns_address)
        self.kube_control.set_dns_domain(dns_domain)
        self.kube_control.set_dns_enabled(dns_enabled)
        self.kube_control.set_dns_port(dns_port)
        self.kube_control.set_has_external_cloud_provider(self.external_cloud_provider.has_xcp)
        self.kube_control.set_image_registry(self.model.config["image-registry"])
        # TODO: labels
        self.kube_control.set_labels([])
        self.kube_control.set_taints(self.model.config["register-with-taints"].split())

        if self.unit.is_leader():
            client_token = auth_webhook.get_token("admin")
            proxy_token = auth_webhook.get_token("system:kube-proxy")

            for request in self.kube_control.auth_requests:
                kubelet_token = auth_webhook.create_token(
                    uid=request.unit, username=request.user, groups=[request.group]
                )
                self.kube_control.sign_auth_request(
                    request,
                    client_token=client_token,
                    kubelet_token=kubelet_token,
                    proxy_token=proxy_token,
                )
        else:
            self.kube_control.clear_creds()

    def configure_kube_proxy(self):
        kubernetes_snaps.configure_kube_proxy(
            cluster_cidr=self.cni.cidr,
            extra_args_config=self.model.config["proxy-extra-args"],
            extra_config=yaml.safe_load(self.model.config["proxy-extra-config"]),
            kubeconfig="/root/cdk/kubeproxyconfig",
            external_cloud_provider=self.external_cloud_provider,
        )

    def configure_kubelet(self):
        kubernetes_snaps.configure_kubelet(
            container_runtime_endpoint=self.container_runtime.socket,
            dns_domain=self.get_dns_domain(),
            dns_ip=self.get_dns_address(),
            extra_args_config=self.model.config["kubelet-extra-args"],
            extra_config=yaml.safe_load(self.model.config["kubelet-extra-config"]),
            external_cloud_provider=self.external_cloud_provider,
            kubeconfig="/root/cdk/kubeconfig",
            node_ip=self.kube_control.ingress_addresses[0],
            registry=self.model.config["image-registry"],
            taints=self.model.config["register-with-taints"].split(),
        )

    def configure_loadbalancers(self):
        if not self.unit.is_leader():
            return

        if self.lb_external.is_available:
            req = self.lb_external.get_request("api-server-external")
            req.protocol = req.protocols.tcp
            req.port_mapping = {443: 6443}
            req.public = True
            if not req.health_checks:
                req.add_health_check(protocol=req.protocols.http, port=6443, path="/livez")
            self.lb_external.send_request(req)

        if self.lb_internal.is_available:
            req = self.lb_internal.get_request("api-server-internal")
            req.protocol = req.protocols.tcp
            req.port_mapping = {6443: 6443}
            req.public = False
            if not req.health_checks:
                req.add_health_check(protocol=req.protocols.http, port=6443, path="/livez")
            self.lb_internal.send_request(req)

    def configure_scheduler(self):
        kubernetes_snaps.configure_scheduler(
            extra_args_config=self.model.config["scheduler-extra-args"],
            kubeconfig="/root/cdk/kubeschedulerconfig",
        )

    def create_kubeconfigs(self):
        ca = self.certificates.ca
        fqdn = self.external_cloud_provider.name == "aws"
        local_server = self.k8s_api_endpoints.local()
        node_name = kubernetes_snaps.get_node_name(fqdn)
        public_server = self.k8s_api_endpoints.external()

        if not os.path.exists("/root/.kube/config"):
            # Create a bootstrap client config. This initial config will allow
            # us to get and create auth webhook tokens via the Kubernetes API,
            # but will not have the final admin token just yet.
            kubernetes_snaps.create_kubeconfig(
                "/root/.kube/config",
                ca=ca,
                server=local_server,
                user="admin",
                token=auth_webhook.token_generator(),
            )

        admin_token = auth_webhook.create_token(
            uid="admin",
            username="admin",
            # wokeignore:rule=master
            groups=["system:masters"],
        )

        for dest in ["/root/.kube/config", "/home/ubuntu/.kube/config"]:
            kubernetes_snaps.create_kubeconfig(
                dest,
                ca=ca,
                server=local_server,
                token=admin_token,
                user="admin",
            )

        kubernetes_snaps.create_kubeconfig(
            "/home/ubuntu/config",
            ca=ca,
            server=public_server,
            token=admin_token,
            user="admin",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubecontrollermanagerconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid="kube-controller-manager", username="system:kube-controller-manager", groups=[]
            ),
            user="kube-controller-manager",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubeschedulerconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid="system:kube-scheduler", username="system:kube-scheduler", groups=[]
            ),
            user="kube-scheduler",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubeconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid=self.unit.name,
                username=f"system:node:{node_name.lower()}",
                groups=["system:nodes"],
            ),
            user="kubelet",
        )

        kubernetes_snaps.create_kubeconfig(
            "/root/cdk/kubeproxyconfig",
            ca=ca,
            server=local_server,
            token=auth_webhook.create_token(
                uid="kube-proxy", username="system:kube-proxy", groups=[]
            ),
            user="kube-proxy",
        )

    def get_cluster_name(self):
        peer_relation = self.model.get_relation("peer")
        cluster_name = peer_relation.data[self.app].get("cluster-name")

        if cluster_name:
            return cluster_name

        if not self.unit.is_leader():
            status.add(WaitingStatus("Waiting for cluster name from leader"))
            return None

        # Check for old cluster name in leader data
        cluster_name = leader_data.get("cluster_tag")
        if cluster_name:
            peer_relation.data[self.app]["cluster-name"] = cluster_name
            leader_data.set("cluster_tag", "")
            return cluster_name

        cluster_name = f"kubernetes-{auth_webhook.token_generator().lower()}"
        peer_relation.data[self.app]["cluster-name"] = cluster_name
        return cluster_name

    def get_dns_address(self):
        return self.kube_dns.address or self.cdk_addons.get_dns_address()

    def get_dns_domain(self):
        return self.kube_dns.domain or self.model.config["dns_domain"]

    def get_dns_port(self):
        return self.kube_dns.port or 53

    def reconcile(self, event):
        """Reconcile state change events."""
        kubernetes_snaps.install(channel=self.model.config["channel"], control_plane=True)
        kubernetes_snaps.configure_services_restart_always(control_plane=True)
        self.request_certificates()
        self.write_certificates()
        self.write_etcd_client_credentials()
        self.write_service_account_key()
        self.configure_auth_webhook()
        self.configure_loadbalancers()
        if self.api_dependencies_ready():
            self.configure_apiserver()
            self.create_kubeconfigs()
            self.configure_controller_manager()
            self.configure_scheduler()
            self.cdk_addons.configure()
            self.configure_container_runtime()
            self.configure_cni()
            self.configure_kernel_parameters()
            self.configure_kubelet()
            self.configure_kube_proxy()
            self.configure_kube_control()

    def request_certificates(self):
        """Request client and server certificates."""
        if not self.certificates.relation:
            status.add(BlockedStatus("Missing relation to certificate authority"))
            return

        bind_addrs = kubernetes_snaps.get_bind_addresses()
        common_name = kubernetes_snaps.get_public_address()
        domain = self.get_dns_domain()
        extra_sans = self.config["extra_sans"].split()
        k8s_service_addrs = kubernetes_snaps.get_kubernetes_service_addresses(
            self.config["service-cidr"].split(",")
        )
        ingress_addrs = self.kube_control.ingress_addresses

        sans = [
            # The CN field is checked as a hostname, so if it's an IP, it
            # won't match unless also included in the SANs as an IP field.
            common_name,
            "127.0.0.1",
            socket.gethostname(),
            socket.getfqdn(),
            "kubernetes",
            f"kubernetes.{domain}",
            "kubernetes.default",
            "kubernetes.default.svc",
            f"kubernetes.default.svc.{domain}",
        ]
        sans += bind_addrs
        sans += ingress_addrs
        sans += k8s_service_addrs
        sans += extra_sans
        sans = list(set(sans))

        self.certificates.request_client_cert("system:kube-apiserver")
        self.certificates.request_server_cert(cn=common_name, sans=sans)

    def write_service_account_key(self):
        peer_relation = self.model.get_relation("peer")
        key = peer_relation.data[self.app].get("service-account-key")

        if key:
            kubernetes_snaps.write_service_account_key(key)
            return

        if not self.unit.is_leader():
            status.add(WaitingStatus("Waiting for key from leader"))
            return

        # Check for old key in leader data
        key = leader_data.get("/root/cdk/serviceaccount.key")
        if key:
            peer_relation.data[self.app]["service-account-key"] = key
            leader_data.set("/root/cdk/serviceaccount.key", "")
            return

        key = kubernetes_snaps.create_service_account_key()
        peer_relation.data[self.app]["service-account-key"] = key

    def write_certificates(self):
        """Write certificates from the certificates relation."""
        common_name = kubernetes_snaps.get_public_address()
        ca = self.certificates.ca
        client_cert = self.certificates.client_certs_map.get("system:kube-apiserver")
        server_cert = self.certificates.server_certs_map.get(common_name)

        if not ca or not client_cert or not server_cert:
            status.add(WaitingStatus("Waiting for certificates"))
            return

        kubernetes_snaps.write_certificates(
            ca=ca,
            client_cert=client_cert.cert,
            client_key=client_cert.key,
            server_cert=server_cert.cert,
            server_key=server_cert.key,
        )

    def write_etcd_client_credentials(self):
        """Write etcd client credentials from the etcd relation."""
        if not self.etcd.relation:
            status.add(BlockedStatus("Missing relation to etcd"))
            return

        if not self.etcd.is_ready:
            status.add(WaitingStatus("Waiting for etcd"))
            return

        creds = self.etcd.get_client_credentials()

        kubernetes_snaps.write_etcd_client_credentials(
            ca=creds["client_ca"], cert=creds["client_cert"], key=creds["client_key"]
        )


if __name__ == "__main__":  # pragma: nocover
    ops.main(KubernetesControlPlaneCharm)