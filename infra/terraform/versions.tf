terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # The SAME state bucket and lock table as relational-fluency (created by its
  # infra/scripts/add-deployer.sh), under this stack's OWN key. Never
  # platform/terraform.tfstate: that is step 1's state, and pointing this stack
  # at it would make the first plan propose destroying all of step 1.
  # tests/test_terraform.py refuses any other key.
  backend "s3" {
    bucket         = "relational-fluency-tfstate-540586745717"
    key            = "rater/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "relational-fluency-tflock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Study     = "IRB0151104"
      Step      = "2"
    }
  }
}
