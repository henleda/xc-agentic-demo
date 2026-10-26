#!/bin/bash
# Export what XC Terraform (deploy/xc/terraform) needs from the repo-root .env, without printing any of it:
#   VES_P12_PASSWORD (read by the provider itself) and TF_VAR_* for the provider and for infra.tf.
# Usage: source scripts/xc_env.sh   (then terraform -chdir=deploy/xc/terraform plan ...)

_xc_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
# Same rules as scripts/envfile.py: a quoted value is taken as-is, otherwise an inline " # comment" is dropped.
# The password is the exception: it is never quoted and may contain " #", so it is taken whole.
_xc_get() { python3 - "$_xc_root/.env" "$1" <<'EOF'
import re, sys
match = re.search(r"(?m)^" + re.escape(sys.argv[2]) + r"=(.*)$", open(sys.argv[1]).read())
raw = match.group(1) if match else ""
if sys.argv[2] == "XC_API_P12_PASSWORD":
    print(raw)
elif raw.strip()[:1] in ('"', "'") and raw.strip().find(raw.strip()[0], 1) > 0:
    s = raw.strip()
    print(s[1:s.find(s[0], 1)])
else:
    print(re.split(r"\s+#", raw, maxsplit=1)[0].strip())
EOF
}
_xc_path() { local p; p="$(_xc_get "$1")"; echo "${p/#\~/$HOME}"; }

export VES_P12_PASSWORD="$(_xc_get XC_API_P12_PASSWORD)"
export TF_VAR_xc_api_url="$(_xc_get XC_API_URL)"
export TF_VAR_xc_api_p12_file="$(_xc_path XC_API_P12_FILE)"
export TF_VAR_aws_region="$(_xc_get AWS_REGION)"
export TF_VAR_aws_vpc_id="$(_xc_get AWS_VPC_ID)"
export TF_VAR_aws_subnet_ce_outside="$(_xc_get AWS_SUBNET_CE_OUTSIDE)"
export TF_VAR_xc_aws_cred_name="$(_xc_get XC_AWS_CRED_NAME)"
export TF_VAR_xc_re_selector="$(_xc_get XC_RE_SELECTOR)"
_xc_key="$(_xc_path XC_CE_SSH_PUBKEY_FILE)"
export TF_VAR_ce_ssh_pubkey="$([ -f "$_xc_key" ] && cat "$_xc_key")"

# Works when sourced from bash or zsh (no ${!name} indirection, which zsh lacks).
_xc_missing=""
[ -n "$VES_P12_PASSWORD" ] || _xc_missing="$_xc_missing XC_API_P12_PASSWORD"
[ -n "$TF_VAR_xc_api_url" ] || _xc_missing="$_xc_missing XC_API_URL"
[ -n "$TF_VAR_aws_vpc_id" ] || _xc_missing="$_xc_missing AWS_VPC_ID"
[ -n "$TF_VAR_aws_subnet_ce_outside" ] || _xc_missing="$_xc_missing AWS_SUBNET_CE_OUTSIDE"
[ -n "$TF_VAR_xc_aws_cred_name" ] || _xc_missing="$_xc_missing XC_AWS_CRED_NAME"
[ -n "$TF_VAR_xc_re_selector" ] || _xc_missing="$_xc_missing XC_RE_SELECTOR"
[ -n "$TF_VAR_ce_ssh_pubkey" ] || _xc_missing="$_xc_missing XC_CE_SSH_PUBKEY_FILE"
[ -f "$TF_VAR_xc_api_p12_file" ] || _xc_missing="$_xc_missing XC_API_P12_FILE(not a file)"
[ -z "$_xc_missing" ] || echo "xc_env: missing from .env:$_xc_missing" >&2
unset -f _xc_get _xc_path; unset _xc_root _xc_key _xc_missing
