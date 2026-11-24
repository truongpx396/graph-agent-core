output "droplet_ipv4" {
  description = "The droplet's public IP. It is not reserved, so it changes if the droplet is replaced."
  value       = digitalocean_droplet.dev.ipv4_address
}

output "droplet" {
  description = "What `make droplet-*` takes as DEV_DROPLET."
  value       = "${var.deploy_user}@${digitalocean_droplet.dev.ipv4_address}"
}

output "ssh_command" {
  value = "ssh ${var.deploy_user}@${digitalocean_droplet.dev.ipv4_address}"
}

output "tunnel_command" {
  description = "Forwards the front door (the web UI at https://api.localhost:8443/), Grafana, Langfuse, the LiteLLM UI and Open WebUI to this machine; the *.localhost names then work as on a laptop. `make droplet-tunnel` runs it."
  value       = "ssh -N -L 8443:localhost:8443 -L 3300:localhost:3300 -L 3000:localhost:3000 -L 4000:localhost:4000 -L 8080:localhost:8080 ${var.deploy_user}@${digitalocean_droplet.dev.ipv4_address}"
}
