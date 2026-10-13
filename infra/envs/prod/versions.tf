terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Bucket and region are supplied at init time (see backend.hcl.example), because the bucket is
  # created by infra/bootstrap and its name contains the account id.
  backend "s3" {
    key          = "envs/prod/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
