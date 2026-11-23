variable "do_token" {
  description = "DigitalOcean API token (create at https://cloud.digitalocean.com/account/api/tokens). Pass via TF_VAR_do_token or DIGITALOCEAN_TOKEN — never commit a real value."
  type        = string
  sensitive   = true
}

variable "project_name" {
  description = "Name for the DigitalOcean project and prefix for resource names. Different from prod's, so the two roots never claim the same names."
  type        = string
  default     = "graph-agent-core-dev"
}

variable "region" {
  description = "DigitalOcean region slug — see `doctl compute region list`."
  type        = string
  default     = "nyc3"
}

variable "droplet_size" {
  description = "Droplet size slug (`doctl compute size list`). The whole dev stack shares one box: Postgres, Redis, Qdrant, MinIO, LiteLLM, Langfuse, the ML service (about 3 cores and 2 GB alone), the API and workers, the front door, the crawler's headless Chromium, and Prometheus, Loki and Grafana. 16 GB is the floor; the crawler image alone is about 9 GB of disk. Billed hourly: destroy it when you are done."
  type        = string
  default     = "s-8vcpu-16gb"
}

variable "droplet_image" {
  description = "Base OS image slug."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "ssh_key_fingerprints" {
  description = "Fingerprints of SSH keys already uploaded to your DO account (`doctl compute ssh-key list`) that get root access on first boot. Required — DigitalOcean emails a root password instead if this is empty, which this config does not support."
  type        = list(string)

  validation {
    condition     = length(var.ssh_key_fingerprints) > 0
    error_message = "At least one SSH key fingerprint is required."
  }
}

variable "admin_ip_cidrs" {
  description = "CIDR blocks allowed to reach SSH, the only inbound port. Everything else is reached through an SSH tunnel (the dev stack's own passwords are admin/admin and sk-anything, so none of it may face the internet). Find yours with `curl -s ifconfig.me`. Never 0.0.0.0/0."
  type        = list(string)

  validation {
    condition     = !contains(var.admin_ip_cidrs, "0.0.0.0/0") && !contains(var.admin_ip_cidrs, "::/0")
    error_message = "admin_ip_cidrs must not be world-open: SSH is the only way in and the dev stack has no real credentials."
  }
}

variable "deploy_user" {
  description = "Unprivileged Linux user cloud-init creates, owning /opt/graph-agent-core-dev and running `docker compose` there; `make droplet-*` connects as this user."
  type        = string
  default     = "deploy"
}

variable "deploy_ssh_public_key" {
  description = "Public key (the contents of an id_ed25519.pub, say) authorized for var.deploy_user over SSH. Your own key is fine here: unlike prod there is no CI deploying to this droplet."
  type        = string
}
