variable "project" {
  description = "Name prefix for every resource this stack creates"
  type        = string
  default     = "relational-fluency-rater"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "domain_name" {
  description = "Zone delegated to Route 53 by Cornell IT (owned by step 1's account, read here as a data source)"
  type        = string
  default     = "ai-ready-workforce.ai.cornell.edu"
}

variable "app_subdomain" {
  description = "This app's hostname under domain_name"
  type        = string
  default     = "rater"
}

variable "container_image" {
  description = "Full ECR image URI with tag (the short git SHA). Empty until the first image is pushed: the task definition and service are then not created at all, rather than created pointing at an image that does not exist."
  type        = string
  default     = ""
}

variable "desired_count" {
  description = "Tasks. 1: /health's active_sessions answers for one task only, and tools/deploy.sh refuses to plan with more."
  type        = number
  default     = 1
}

# --- step 1, read only ---------------------------------------------------------------

variable "step1_bucket" {
  description = "Step 1's study bucket. READ ONLY: the task role gets GetObject and prefix-scoped ListBucket, and an explicit deny on every write."
  type        = string
  default     = "relational-fluency-study-data"
}

variable "step1_kms_alias" {
  description = "Step 1's study-data KMS key. The task role gets kms:Decrypt on it, via S3 only."
  type        = string
  default     = "alias/relational-fluency-study"
}

variable "step1_read_prefixes" {
  description = "Prefixes of the step 1 bucket this app may read. runs/ is empty until step 1 exports runs (docs/step1-runs-export.md); granting it now costs nothing."
  type        = list(string)
  default     = ["encounters/", "runs/"]
}
