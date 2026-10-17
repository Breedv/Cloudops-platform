terraform {
  required_version = ">= 1.5.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

resource "local_file" "cloudops_info" {
  filename = "${path.module}/cloudops-info.txt"

  content = <<-EOT
    CloudOps Platform
    Infrastructure as Code with Terraform
    Project: Cloud Service Uptime & Incident Management
  EOT
}