# XC tenant f5-amer-ent. Nothing secret is in this file or in git:
#   - the API certificate path is var.xc_api_p12_file (XC_API_P12_FILE in .env, the file lives in ~/.xc/);
#   - its password is the environment variable VES_P12_PASSWORD, which the provider reads itself.
# scripts/xc_env.sh exports TF_VAR_xc_api_url, TF_VAR_xc_api_p12_file and VES_P12_PASSWORD from the repo-root .env.

terraform {
  required_version = ">= 1.6"
  required_providers {
    volterra = { source = "volterraedge/volterra", version = "0.13.2" }
  }
}

variable "xc_api_url" {
  type        = string
  description = "https://<tenant>.console.ves.volterra.io/api (XC_API_URL in .env)"
}

variable "xc_api_p12_file" {
  type        = string
  description = "Absolute path of the API certificate (XC_API_P12_FILE in .env)"
}

provider "volterra" {
  url          = var.xc_api_url
  api_p12_file = var.xc_api_p12_file
}
