# Phase 0 in XC (docs/07 step 4, intent: ../intent/00-infra.yaml): the namespace, the AWS CE site and the request to
# launch it, both virtual sites, and the vK8s object. Fields mapped against the real volterraedge/volterra 0.13.2
# schema (CLAUDE.md rule 2; dump in ../generated/schema-0.13.2/). Open questions: TODO(verify-xc) here and docs/08.

variable "aws_region" {
  type    = string
  default = "us-east-2"
}

variable "aws_az" {
  type    = string
  default = "us-east-2a"
}

variable "aws_vpc_id" {
  type        = string
  description = "AWS_VPC_ID in .env (deploy/aws output)"
}

variable "aws_subnet_ce_site" {
  type        = string
  description = "AWS_SUBNET_CE_SITE in .env (deploy/aws output): the CE's only interface, a subnet with no route table association"
}

variable "xc_aws_cred_name" {
  type        = string
  description = "XC_AWS_CRED_NAME in .env: an AWS cloud credential in namespace system, created in the console (not here: its secret would be in state)"
}

variable "ce_ssh_pubkey" {
  type        = string
  description = "OpenSSH public key for the CE node (XC_CE_SSH_PUBKEY_FILE in .env points at it; the private key stays in ~/.ssh)"
}

variable "xc_re_selector" {
  type        = string
  description = "XC_RE_SELECTOR in .env: which REs run the RE-side agents (label ves.io/siteName, names prefixed ves-io-)"
}

resource "volterra_namespace" "demo" {
  name        = "agentic-demo"
  description = "Copperline XC agentic demo (github.com/henleda/xc-agentic-demo)"
}

# ---- The AWS CE: App Stack (voltstack_cluster), one node, in the existing VPC ------------------------------------
# App Stack has ONE interface here (docs/08 R30). The node sits in ce-site, a subnet with no route table of ours: XC
# creates its route table (default route to the internet gateway, for registration and the tunnels to the REs) and
# refuses a subnet that already has one (R45). The core subnet (the EC2 origins) is reached through the VPC local route.

resource "volterra_aws_vpc_site" "ce" {
  name          = "copperline-aws-ce"
  namespace     = "system" # sites are system objects (henley-aws-site, the tenant's other AWS sites, live there)
  aws_region    = var.aws_region
  instance_type = "m5.2xlarge"
  disk_size     = 100
  ssh_key       = var.ce_ssh_pubkey
  labels        = { "copperline.demo/site" = "aws-ce" } # vs-ce-aws selects on this

  aws_cred {
    name      = var.xc_aws_cred_name
    namespace = "system"
  }

  vpc {
    vpc_id = var.aws_vpc_id
  }

  voltstack_cluster {
    aws_certified_hw = "aws-byol-voltstack-combo" # the only value the API allows for App Stack

    az_nodes {
      aws_az_name = var.aws_az
      local_subnet {
        existing_subnet_id = var.aws_subnet_ce_site
      }
    }

    no_k8s_cluster           = true # vK8s is the separate volterra_virtual_k8s below. TODO(verify-xc): confirm (docs/08 R45)
    no_network_policy        = true
    no_forward_proxy         = true
    no_global_network        = true # origins are on the site-local network in the same VPC
    no_dc_cluster_group      = true
    no_outside_static_routes = true # core is reachable through the VPC local route
    sm_connection_public_ip  = true # no site mesh group, so no effect. TODO(verify-xc): does it allocate an EIP? (R45)
    default_storage          = true # pods use emptyDir only
  }

  no_worker_nodes          = true
  egress_gateway_default   = true # egress through the VPC's internet gateway, on the route table XC creates
  f5xc_security_group      = true
  f5_orchestrated_routing  = true # XC owns ce-site's route table (manual_routing was refused the same way, R45)
  disable_internet_vip     = true # public entry is on the REs
  default_blocked_services = true
  direct_connect_disabled  = true
  logs_streaming_disabled  = true
  disable_encryption       = true

  offline_survivability_mode {
    no_offline_survivability_mode = true
  }
}

# Creating the site object above does not create anything in AWS by itself: XC builds the CE when asked to "apply"
# the site's Terraform parameters. TODO(verify-xc): that this resource is what launches it, the action spelling, and
# what wait_for_action waits for (docs/08 R45). Destroying it should tear the CE down in AWS (same TODO).
resource "volterra_tf_params_action" "ce_apply" {
  site_name       = volterra_aws_vpc_site.ce.name
  site_kind       = "aws_vpc_site"
  action          = "apply"
  wait_for_action = true
}

# ---- Virtual sites (where Deployments land, via the ves.io/virtual-sites annotation) and the vK8s object ---------

resource "volterra_virtual_site" "ce" {
  name      = "vs-ce-aws"
  namespace = volterra_namespace.demo.name
  site_type = "CUSTOMER_EDGE"
  site_selector {
    expressions = ["copperline.demo/site in (aws-ce)"]
  }
}

resource "volterra_virtual_site" "re" {
  name      = "vs-re-agents"
  namespace = volterra_namespace.demo.name
  site_type = "REGIONAL_EDGE"
  site_selector {
    expressions = [var.xc_re_selector] # e.g. "ves.io/siteName in (ves-io-dc12-ash, ...)", as the tenant's own vsites do
  }
}

resource "volterra_virtual_k8s" "vk8s" {
  name      = "agentic-demo-vk8s"
  namespace = volterra_namespace.demo.name
  disabled  = true # service isolation off (intent service_isolation: false); exclusive with `isolated`

  vsite_refs {
    name      = volterra_virtual_site.re.name
    namespace = volterra_virtual_site.re.namespace
  }
  vsite_refs {
    name      = volterra_virtual_site.ce.name
    namespace = volterra_virtual_site.ce.namespace
  }
}
