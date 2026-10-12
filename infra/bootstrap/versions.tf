terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Bootstrap uses local state on purpose: it creates the bucket that holds every other state.
  # After the first apply you may move its own state into that bucket (see infra/README.md).
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "forgeeval"
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}
