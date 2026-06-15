variable "project" {
  description = "Nombre del proyecto, usado como prefijo en los nombres de los recursos del módulo."
  type        = string

  validation {
    condition     = length(var.project) > 0 && length(var.project) <= 24
    error_message = "El nombre de proyecto debe tener entre 1 y 24 caracteres."
  }
}

variable "tags" {
  description = "Tags comunes a propagar a todos los recursos creados por el módulo."
  type        = map(string)
  default     = {}
}

variable "principal_arn" {
  description = "ARN del rol IAM usado como execution role de la Lambda. En AWS Academy se reutiliza LabRole."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:iam::\\d{12}:role/.+$", var.principal_arn))
    error_message = "principal_arn debe ser un ARN de rol IAM válido."
  }
}

variable "vpc_id" {
  description = "ID de la VPC donde se despliega la Lambda."
  type        = string
}

variable "private_subnet_ids" {
  description = "IDs de las subnets privadas de aplicación donde se despliega la Lambda."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 1
    error_message = "Se requiere al menos 1 subnet privada."
  }
}

variable "endpoint_security_group_id" {
  description = "ID del Security Group de los Interface VPC Endpoints; la Lambda abre egress TCP/443 hacia este SG."
  type        = string
}

variable "psycopg2_layer_arn" {
  description = "ARN del Lambda layer con psycopg2 compilado para Amazon Linux 2023 (Python 3.12)."
  type        = string
}

variable "db_host" {
  description = "Hostname del endpoint RDS (DB_HOST en la Lambda)."
  type        = string
}

variable "db_port" {
  description = "Puerto del endpoint RDS (DB_PORT en la Lambda)."
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Nombre de la base de datos PostgreSQL (DB_NAME en la Lambda)."
  type        = string
  default     = "fraud_results"
}

variable "db_username" {
  description = "Usuario master de la base de datos (DB_USER en la Lambda)."
  type        = string
  default     = "fraud_admin"
}

variable "db_password" {
  description = "Contraseña del usuario master de la base de datos."
  type        = string
  sensitive   = true
}

variable "sns_topic_arn" {
  description = "ARN del topic SNS de resúmenes usado para suscripciones email del dashboard."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:sns:[a-z0-9-]+:\\d{12}:.+$", var.sns_topic_arn))
    error_message = "sns_topic_arn debe ser un ARN válido de SNS."
  }
}

variable "user_behavior_table_name" {
  description = "Nombre de la tabla DynamoDB de comportamiento de usuarios consultada por GET /users/{id}/behavior."
  type        = string

  validation {
    condition     = length(var.user_behavior_table_name) > 0
    error_message = "user_behavior_table_name no puede quedar vacío."
  }
}

variable "jwt_issuer" {
  description = "Issuer del JWT de Cognito usado por el authorizer HTTP API."
  type        = string

  validation {
    condition     = can(regex("^https://", var.jwt_issuer))
    error_message = "jwt_issuer debe ser una URL HTTPS válida."
  }
}

variable "jwt_audience" {
  description = "Audience del JWT de Cognito usado por el authorizer HTTP API."
  type        = string

  validation {
    condition     = length(var.jwt_audience) > 0
    error_message = "jwt_audience no puede quedar vacío."
  }
}

variable "package_file" {
  description = "Ruta absoluta al paquete .zip de la Lambda API, generado desde app/api/handler.py en la composición raíz."
  type        = string

  validation {
    condition     = length(var.package_file) > 0 && endswith(var.package_file, ".zip")
    error_message = "package_file debe ser la ruta a un archivo .zip existente."
  }
}

variable "timeout_seconds" {
  description = "Timeout de la Lambda API en segundos."
  type        = number
  default     = 30

  validation {
    condition     = var.timeout_seconds >= 1 && var.timeout_seconds <= 900
    error_message = "timeout_seconds debe estar entre 1 y 900 segundos."
  }
}

variable "log_retention_days" {
  description = "Días de retención de los logs en CloudWatch Logs."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653], var.log_retention_days)
    error_message = "log_retention_days debe ser un valor permitido por CloudWatch Logs."
  }
}
