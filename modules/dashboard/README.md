# `modules/dashboard`

Provisions the S3 static website bucket used by the fraud dashboard. Terraform owns the bucket, website configuration, public-read policy for the lab, bootstrap object keys, and outputs. CI builds `app/dashboard/Dockerfile` and syncs the exported files to this Terraform-created bucket. The bootstrap `config.js` now also carries Cognito settings for the dashboard auth slice.

## Resources

- `aws_s3_bucket.dashboard` — `<project>-dashboard-<account-id>`, force-destroy enabled for the short-lived lab. The account suffix avoids S3 global-name collisions across AWS Academy labs and keeps the Cognito callback URL stable for the active account.
- `aws_s3_bucket_versioning.dashboard` — versioning enabled on the static website bucket.
- `aws_s3_bucket_website_configuration.dashboard` — static website hosting with `index.html`.
- `aws_s3_bucket_public_access_block.dashboard` — public access block relaxed for the public website endpoint.
- `aws_s3_bucket_policy.dashboard` — allows public `s3:GetObject` on website objects.
- `aws_s3_object.index_html`, `aws_s3_object.app_js`, `aws_s3_object.favicon_ico`, `aws_s3_object.config_js` — bootstrap dashboard objects. These stable filenames are published with `no-cache, no-store, must-revalidate` so browsers do not keep stale Cognito/API settings after a redeploy. `favicon_ico` is sourced from `favicon.ico` alongside `index.html` (same directory as `index_html_path`).

## Inputs

| Name      | Type          | Default | Description                                      |
| --------- | ------------- | ------- | ------------------------------------------------ |
| `project` | `string`      | n/a     | Prefix for the dashboard bucket name; the module appends the current AWS account id. |
| `index_html_path` | `string` | n/a | Absolute path to `app/dashboard/index.html`. The module also uploads `favicon.ico` from the same directory. |
| `app_js_path` | `string` | n/a | Absolute path to `app/dashboard/app.js`. |
| `config_js_template_path` | `string` | n/a | Absolute path to `app/dashboard/config.js.tpl`. |
| `api_endpoint` | `string` | n/a | API Gateway endpoint used for the bootstrap `config.js`. |
| `cognito_user_pool_id` | `string` | n/a | Cognito user pool ID written into `config.js`. |
| `cognito_client_id` | `string` | n/a | Cognito app client ID written into `config.js`. |
| `cognito_domain_url` | `string` | n/a | Cognito managed domain URL written into `config.js`. |
| `cognito_hosted_ui_base_url` | `string` | n/a | Cognito Hosted UI authorize URL written into `config.js`. |
| `cognito_issuer` | `string` | n/a | Cognito issuer URL written into `config.js`. |
| `cognito_redirect_uri` | `string` | n/a | Cognito callback URL written into `config.js`. |
| `cognito_logout_uri` | `string` | n/a | Cognito logout URL written into `config.js`. |
| `tags`    | `map(string)` | `{}`    | Common tags merged with `Component = "dashboard"`. |

## Outputs

| Name          | Description                                      |
| ------------- | ------------------------------------------------ |
| `website_url` | HTTP S3 website endpoint URL. Do not use it as the Cognito login entrypoint. |
| `https_index_url` | HTTPS S3 object URL for `index.html`, used by Cognito callback/logout and browser PKCE. |
| `bucket_name` | Bucket name where CI syncs the frontend export.  |

## Deployment

The Deploy workflow builds the dashboard export with the current API Gateway endpoint:

```bash
docker build \
  -f app/dashboard/Dockerfile \
  --target export \
  --build-arg "API_BASE=$(terraform output -raw api_endpoint)" \
  --build-arg "COGNITO_CLIENT_ID=$(terraform output -raw cognito_client_id)" \
  --build-arg "COGNITO_DOMAIN=$(terraform output -raw cognito_domain_url)" \
  --build-arg "COGNITO_REDIRECT_URI=$(terraform output -raw dashboard_app_url)" \
  --build-arg "COGNITO_LOGOUT_URI=$(terraform output -raw dashboard_app_url)" \
  --build-arg "COGNITO_USER_POOL_ID=$(terraform output -raw cognito_user_pool_id)" \
  --build-arg "COGNITO_ISSUER=$(terraform output -raw cognito_issuer)" \
  --output type=local,dest=dashboard-dist \
  app

aws s3 sync dashboard-dist "s3://$(terraform output -raw dashboard_bucket_name)/" \
  --delete \
  --cache-control "no-cache, no-store, must-revalidate"
```

Terraform uploads `favicon.ico` during `terraform apply`, and `app/dashboard/Dockerfile` includes the same file in `dashboard-dist` so the Deploy workflow keeps it during `aws s3 sync --delete`.
