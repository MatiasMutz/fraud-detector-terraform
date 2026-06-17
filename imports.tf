# Import blocks for resources that were auto-created by AWS before Terraform
# managed them. These are one-shot: once the resource is in state the import
# block is a no-op on subsequent plans.

# Lambda auto-creates its log group on first invocation, so it may already
# exist before Terraform gets to create it.
import {
  to = module.results_writer.aws_cloudwatch_log_group.writer
  id = "/aws/lambda/itba-tp-fraud-results-writer"
}
