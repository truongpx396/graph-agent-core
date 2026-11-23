# The dev environment on ONE DigitalOcean droplet: the whole dev stack (deploy/dev/: infra, the app, the front door, the crawler, observability) on a single
# machine, with a hosted LLM endpoint instead of the laptop's Ollama. Production is the two-droplet stack in ../prod/. See deploy/README.md, "The dev droplet".
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.42"
    }
  }

  # Local state, as in ../prod/versions.tf (which has the Spaces backend block to copy if more than one person applies). This is a different root with its own state:
  # never point both at one `key`.
}

provider "digitalocean" {
  token = var.do_token
}
