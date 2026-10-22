# A VPC of this stack's own, NOT step 1's. The rater app shares nothing on the
# network path with participant collection: a mistake here cannot reach
# rf.ai-ready-workforce.ai.cornell.edu's ALB, listener rules, subnets or NAT.
#
# No NAT gateway (step 1 pays ~$33/month for one). Tasks run in the public
# subnets with a public IPv4 so they can reach ECR, Secrets Manager and
# CloudWatch Logs, and the task security group admits nothing but the ALB.
# S3, the only data path, goes through a free gateway endpoint. Interface
# endpoints for the other three would cost more than the NAT they replace.
#
# 10.41/16, not step 1's 10.40/16, so the two could be peered later.

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = var.project
  cidr = "10.41.0.0/16"

  azs            = ["${var.region}a", "${var.region}b"]
  public_subnets = ["10.41.0.0/20", "10.41.16.0/20"]

  enable_nat_gateway      = false
  enable_dns_hostnames    = true
  map_public_ip_on_launch = false
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.public_route_table_ids
}

resource "aws_security_group" "alb" {
  name_prefix = "${var.project}-alb-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTPS from anywhere (participants arrive from Qualtrics)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTP (redirected to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "To the tasks"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "app" {
  name_prefix = "${var.project}-app-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "App port from the ALB only"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  # HTTPS out only: ECR, Secrets Manager, CloudWatch Logs and S3 are all 443.
  # The app calls no model gateway and nothing else.
  egress {
    description = "AWS APIs over HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}
