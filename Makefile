PSYCOPG2_VERSION := 2.9.10
PSYCOPG2_ZIP     := layers/psycopg2/psycopg2-layer.zip

RESULTS_WRITER_DIR := app/results_writer
RESULTS_WRITER_ZIP := $(RESULTS_WRITER_DIR)/build/results-writer.zip

MODEL_PREP_SOURCE := app/net/outputs/go_runtime/model_v1/runtime_spec.json
MODEL_PREP_DEST := app/processor/model/runtime_spec.json
MODEL_PREP_FALLBACK_FILE_URL := https://drive.google.com/file/d/1Gut3LFjfYVpIEHJIXkzztcfZ6o-CRAwX/view?usp=sharing
MODEL_PREP_FALLBACK_URL := https://drive.google.com/drive/folders/1DfGgK6dTXP-IS3bdqSl_kBCIkr6P9NR6?usp=sharing

LAMBDA_WRITER ?= itba-tp-fraud-results-writer
API_LAMBDA    ?=
ONPREM_STACK  ?= itba-tp-fraud-onprem-strongswan
LOG_GROUP     ?= /ecs/itba-tp-fraud-fraud-engine
REGION        ?= $(if $(AWS_REGION),$(AWS_REGION),us-east-1)
COUNT         ?= 1000
DAYS          ?= 30
TX_COUNT      ?= 50000
FRAUD_PCT     ?= 20
TX_CONCURRENCY ?= 128
PYTHON        ?= $(shell which python3)
GOOGLE_OAUTH_CLIENT_ID     ?=
GOOGLE_OAUTH_CLIENT_SECRET ?=
BOOTSTRAP_EMAIL            ?=
BOOTSTRAP_PASSWORD         ?=
BOOTSTRAP_DISPLAY_NAME     ?= Bootstrap Admin

.PHONY: help fmt fmt-check validate lint init plan apply destroy clean build-layers build-results-writer prepare-model model-prep migrate-db seed bootstrap-auth send-test-tx logs

help:
	@echo "Targets:"
	@echo "  make fmt              Format all Terraform files (in-place)"
	@echo "  make fmt-check        Check formatting without modifying files (for CI)"
	@echo "  make validate         Validate all Terraform files"
	@echo "  make lint             Run checkov (Terraform)"
	@echo "  make build-layers     Build psycopg2 Lambda layer (required before first plan)"
	@echo "  make build-results-writer Build the Go results-writer Lambda package"
	@echo "  make prepare-model    Prepare app/processor/model/runtime_spec.json for container builds"
	@echo "  make init             Initialize the working directory"
	@echo "  make plan             Plan (writes tfplan)"
	@echo "  make apply            Apply the saved plan"
	@echo "  make migrate-db       Invoke the API Lambda direct migration action"
	@echo "  make destroy          Destroy all managed infrastructure"
	@echo "  make clean            Remove .terraform/ and tfplan files"
	@echo "  make seed             Seed RDS with ~COUNT mock transactions (default COUNT=1000, DAYS=30)"
	@echo "  make bootstrap-auth   Bootstrap dashboard admin access with BOOTSTRAP_EMAIL"
	@echo "  make send-test-tx     Send TX_COUNT real transactions via on-prem EC2 → VPN → SQS (default TX_COUNT=50000, FRAUD_PCT=20, TX_CONCURRENCY=128)"
	@echo "  make logs             Tail Fargate worker logs in real time (Ctrl+C to stop)"

fmt:
	terraform fmt -recursive

fmt-check:
	terraform fmt -check -recursive

validate: $(PSYCOPG2_ZIP) build-results-writer
	@mkdir -p app/api/build app/notification/build
	@for d in $$(find . -type f -name '*.tf' -not -path '*/.*' -exec dirname {} \; | sort -u); do \
		echo "==> $$d"; \
		if [ "$$d" = "./modules/dashboard" ]; then \
			(cd $$d && terraform init -backend=false -input=false >/dev/null && terraform validate \
			  -var="index_html_path=$$(pwd)/app/dashboard/index.html" \
			  -var="app_js_path=$$(pwd)/app/dashboard/app.js" \
			  -var="config_js_template_path=$$(pwd)/app/dashboard/config.js.tpl" \
			  -var="api_endpoint=https://example.com" \
			  -var="cognito_user_pool_id=us-east-1_example" \
			  -var="cognito_client_id=example" \
			  -var="cognito_domain_url=https://example.auth.us-east-1.amazoncognito.com" \
			  -var="cognito_hosted_ui_base_url=https://example.auth.us-east-1.amazoncognito.com" \
			  -var="cognito_issuer=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example" \
			  -var="cognito_redirect_uri=https://example.com/" \
			  -var="cognito_logout_uri=https://example.com/") || exit 1; \
		elif [ "$$d" = "./modules/api" ]; then \
			(cd $$d && terraform init -backend=false -input=false >/dev/null && terraform validate \
			  -var="package_file=$$(pwd)/app/api/build/api.zip" \
			  -var="project=test" \
			  -var="principal_arn=arn:aws:iam::123456789012:role/LabRole" \
			  -var="vpc_id=vpc-123" \
			  -var="private_subnet_ids=[\"subnet-1\"]" \
			  -var="endpoint_security_group_id=sg-123" \
			  -var="psycopg2_layer_arn=arn:aws:lambda:us-east-1:123456789012:layer:psycopg2:1" \
			  -var="db_host=localhost" \
			  -var="db_password=secret" \
			  -var="sns_topic_arn=arn:aws:sns:us-east-1:123456789012:topic" \
			  -var="user_behavior_table_name=test-user-behavior" \
			  -var="jwt_issuer=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example" \
			  -var="jwt_audience=client") || exit 1; \
		elif [ "$$d" = "./modules/notification/summarizer" ]; then \
			(cd $$d && terraform init -backend=false -input=false >/dev/null && terraform validate \
			  -var="package_file=$$(pwd)/app/notification/build/summarizer.zip" \
			  -var="project=test" \
			  -var="principal_arn=arn:aws:iam::123456789012:role/LabRole" \
			  -var="vpc_id=vpc-123" \
			  -var="private_subnet_ids=[\"subnet-1\"]" \
			  -var="endpoint_security_group_id=sg-123" \
			  -var="fraud_alert_queue_url=https://sqs.us-east-1.amazonaws.com/123/queue" \
			  -var="summary_topic_arn=arn:aws:sns:us-east-1:123456789012:topic") || exit 1; \
		else \
			(cd $$d && terraform init -backend=false -input=false >/dev/null && terraform validate) || exit 1; \
		fi; \
	done

lint:
	checkov -d . --framework terraform --quiet --compact

# Builds the psycopg2 Lambda layer zip using a manylinux wheel
$(PSYCOPG2_ZIP):
	@echo "Building psycopg2 layer (version $(PSYCOPG2_VERSION))..."
	@mkdir -p layers/psycopg2/python
	@python3 -m pip install "psycopg2-binary==$(PSYCOPG2_VERSION)" \
	  --platform manylinux2014_x86_64 \
	  --only-binary=:all: \
	  --python-version 312 \
	  --implementation cp \
	  --target layers/psycopg2/python \
	  --quiet
	@cd layers/psycopg2 && zip -r psycopg2-layer.zip python/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null
	@echo "Layer ready: $(PSYCOPG2_ZIP)"

build-layers: $(PSYCOPG2_ZIP)

build-results-writer:
	@echo "Building results-writer Lambda..."
	@mkdir -p $(RESULTS_WRITER_DIR)/build
	@cd $(RESULTS_WRITER_DIR) && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o build/bootstrap ./cmd/bootstrap
	@cd $(RESULTS_WRITER_DIR)/build && zip -q -j results-writer.zip bootstrap
	@echo "Lambda package ready: $(RESULTS_WRITER_ZIP)"

init:
	ACCOUNT_ID=$$(aws sts get-caller-identity --query Account --output text) && \
	BUCKET="itba-tp-fraud-tfstate-$${ACCOUNT_ID}" && \
	(aws s3api head-bucket --bucket "$${BUCKET}" 2>/dev/null || \
	  aws s3api create-bucket --bucket "$${BUCKET}" --region us-east-1) && \
	aws s3api put-bucket-versioning \
	  --bucket "$${BUCKET}" \
	  --versioning-configuration Status=Enabled && \
	terraform init -migrate-state -force-copy -backend-config="bucket=$${BUCKET}"

plan: $(PSYCOPG2_ZIP) build-results-writer
	@mkdir -p app/api/build app/notification/build
	TF_VAR_google_oauth_client_id="$(GOOGLE_OAUTH_CLIENT_ID)" \
	TF_VAR_google_oauth_client_secret="$(GOOGLE_OAUTH_CLIENT_SECRET)" \
	terraform plan -out=tfplan

apply:
	terraform apply tfplan

destroy:
	terraform destroy
	@aws lambda list-event-source-mappings --region us-east-1 \
	  --query 'EventSourceMappings[?contains(FunctionArn, `itba-tp-fraud`)].UUID' \
	  --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$$' | \
	  xargs -I{} aws lambda delete-event-source-mapping --uuid {} --region us-east-1 2>/dev/null || true

clean:
	find . -type d -name ".terraform" -exec rm -rf {} +
	find . -type f -name "tfplan" -delete

prepare-model:
	python3 scripts/prepare_runtime_spec.py \
	  --source "$(MODEL_PREP_SOURCE)" \
	  --dest "$(MODEL_PREP_DEST)" \
	  --drive-file-url "$(MODEL_PREP_FALLBACK_FILE_URL)" \
	  --drive-folder-url "$(MODEL_PREP_FALLBACK_URL)"

model-prep: prepare-model

migrate-db:
	@if [ -n "$(API_LAMBDA)" ]; then \
	  $(PYTHON) scripts/invoke_db_migration.py --function "$(API_LAMBDA)" --region "$(REGION)"; \
	else \
	  $(PYTHON) scripts/invoke_db_migration.py --region "$(REGION)"; \
	fi

seed:
	$(PYTHON) scripts/generate_mock_data.py \
	  --function $(LAMBDA_WRITER) \
	  --region $(REGION) \
	  --count $(COUNT) \
	  --days $(DAYS)

bootstrap-auth:
	@if [ -z "$(BOOTSTRAP_EMAIL)" ]; then \
	  echo "BOOTSTRAP_EMAIL is required, e.g. make bootstrap-auth BOOTSTRAP_EMAIL=you@example.com"; \
	  exit 1; \
	fi
	$(PYTHON) scripts/bootstrap_auth.py \
	  --email "$(BOOTSTRAP_EMAIL)" \
	  --password "$(BOOTSTRAP_PASSWORD)" \
	  --display-name "$(BOOTSTRAP_DISPLAY_NAME)" \
	  --alert-email "$(BOOTSTRAP_EMAIL)" \
	  --region "$(REGION)"

send-test-tx:
	$(PYTHON) scripts/send_test_transactions.py \
	  --stack $(ONPREM_STACK) \
	  --region $(REGION) \
	  --count $(TX_COUNT) \
	  --fraud-pct $(FRAUD_PCT) \
	  --concurrency $(TX_CONCURRENCY)

logs:
	aws logs tail $(LOG_GROUP) --follow --region $(REGION)
