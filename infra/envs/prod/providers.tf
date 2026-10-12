provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "forgeeval"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}
