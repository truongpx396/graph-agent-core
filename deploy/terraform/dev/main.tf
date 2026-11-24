resource "digitalocean_project" "this" {
  name        = var.project_name
  description = "graph-agent-core dev stack on one droplet (see deploy/dev/)"
  purpose     = "Web Application"
  environment = "Development"
}

# The whole dev stack. No reserved IP, VPC or domain: nothing public depends on this box, and it is reached over SSH (and tunnels through it) only.
resource "digitalocean_droplet" "dev" {
  name       = var.project_name
  region     = var.region
  size       = var.droplet_size
  image      = var.droplet_image
  ssh_keys   = var.ssh_key_fingerprints
  monitoring = true
  backups    = false # a dev box is rebuilt from the repo, not restored

  user_data = templatefile("${path.module}/../cloud-init.tpl.yaml", {
    deploy_user           = var.deploy_user
    deploy_ssh_public_key = var.deploy_ssh_public_key
    # No inbound port beyond SSH, which the template's `ufw allow OpenSSH` already opens.
    ufw_tcp_ports = []
    app_dir       = "/opt/graph-agent-core-dev"
    # `make` runs the same targets as a laptop (`make droplet-up` calls `make up-all` there); `rsync` is how the repo gets onto the box.
    extra_packages = ["make", "rsync"]
  })

  # cloud-init runs on first boot only (see ../prod/main.tf's note): to pick up a change, replace the droplet.
  lifecycle {
    ignore_changes = [image]
  }
}

resource "digitalocean_project_resources" "this" {
  project   = digitalocean_project.this.id
  resources = [digitalocean_droplet.dev.urn]
}

# The cloud firewall is the first layer (ufw on the droplet the second). Default-deny: SSH from the operator's addresses, nothing else. The dev compose stack publishes its
# services on every interface, as it does on a laptop, so NEVER add an inbound rule here: reach them through `ssh -L` (see the `tunnel_command` output).
resource "digitalocean_firewall" "dev" {
  name        = "${var.project_name}-fw"
  droplet_ids = [digitalocean_droplet.dev.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.admin_ip_cidrs
  }

  # Outbound is open: image pulls, apt, and the hosted LLM endpoint.
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
