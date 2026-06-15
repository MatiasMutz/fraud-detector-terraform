variable "image_uri" {
  description = "URI completa de la imagen de contenedor que ejecuta el scoring de fraude (incluye etiqueta o digest). El push de la imagen ocurre fuera de Terraform."
  type        = string
  default     = ""
}

variable "task_cpu" {
  description = "CPU asignada a cada task de Fargate, expresada en unidades del task definition (1024 = 1 vCPU)."
  type        = number
  default     = 512

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.task_cpu)
    error_message = "task_cpu debe ser uno de los valores soportados por Fargate: 256, 512, 1024, 2048, 4096."
  }
}

variable "task_memory" {
  description = "Memoria asignada a cada task de Fargate, en MiB."
  type        = number
  default     = 1024

  validation {
    condition     = var.task_memory >= 512 && var.task_memory <= 30720
    error_message = "task_memory debe estar entre 512 y 30720 MiB."
  }
}

variable "desired_count" {
  description = "Cantidad inicial de tasks que el servicio ECS mantiene corriendo."
  type        = number
  default     = 2

  validation {
    condition     = var.desired_count >= 0
    error_message = "desired_count no puede ser negativo."
  }
}

variable "min_capacity" {
  description = "Cantidad mínima de tasks que el autoscaling puede mantener en el servicio."
  type        = number
  default     = 1

  validation {
    condition     = var.min_capacity >= 0
    error_message = "min_capacity no puede ser negativo."
  }
}

variable "max_capacity" {
  description = "Cantidad máxima de tasks que el autoscaling puede levantar ante picos de carga."
  type        = number
  default     = 10

  validation {
    condition     = var.max_capacity >= 1
    error_message = "max_capacity debe ser al menos 1."
  }
}

variable "processor_concurrency" {
  description = "Cantidad de workers concurrentes por task de Fargate para procesar mensajes SQS."
  type        = number
  default     = 32

  validation {
    condition     = var.processor_concurrency >= 1 && var.processor_concurrency <= 512
    error_message = "processor_concurrency debe estar entre 1 y 512."
  }
}

variable "processor_pollers" {
  description = "Cantidad de long-pollers SQS concurrentes por task de Fargate."
  type        = number
  default     = 4

  validation {
    condition     = var.processor_pollers >= 1 && var.processor_pollers <= 64
    error_message = "processor_pollers debe estar entre 1 y 64."
  }
}

variable "results_writer_batch_size" {
  description = "Cantidad máxima de mensajes SQS que cada invocación de la Lambda results-writer recibe para insertar en bloque en RDS."
  type        = number
  default     = 5000

  validation {
    condition     = var.results_writer_batch_size >= 1 && var.results_writer_batch_size <= 10000
    error_message = "results_writer_batch_size debe estar entre 1 y 10000 para event source mappings SQS estándar."
  }
}

variable "results_writer_batching_window_seconds" {
  description = "Segundos máximos que Lambda espera para acumular mensajes antes de invocar results-writer."
  type        = number
  default     = 5

  validation {
    condition     = var.results_writer_batching_window_seconds >= 1 && var.results_writer_batching_window_seconds <= 300
    error_message = "results_writer_batching_window_seconds debe estar entre 1 y 300 segundos; batch sizes mayores a 10 requieren al menos 1 segundo."
  }
}

variable "api_lambda_timeout_seconds" {
  description = "Timeout de la Lambda API en segundos. Aumentarlo permite consultas agregadas del dashboard con más margen antes de que API Gateway reciba un 500 por timeout."
  type        = number
  default     = 30

  validation {
    condition     = var.api_lambda_timeout_seconds >= 1 && var.api_lambda_timeout_seconds <= 900
    error_message = "api_lambda_timeout_seconds debe estar entre 1 y 900 segundos."
  }
}

variable "enable_onprem_sim" {
  description = "Habilita la VPC simulada de on-premise con su EC2 strongSwan, Customer Gateway, conexión Site-to-Site VPN contra el VGW y la Private Hosted Zone para SQS. La ingesta queda acotada arquitecturalmente por VPN + VPCE + SG (sin condiciones aws:SourceVpc en la cola)."
  type        = bool
  default     = true
}

variable "enable_onprem_traffic_producers" {
  description = "Crea dos instancias EC2 en la VPC on-premise simulada que envían transacciones sintéticas de forma continua a la cola SQS de ingesta. Requiere enable_onprem_sim = true."
  type        = bool
  default     = true
}

variable "fraud_alert_summary_interval_minutes" {
  description = "Intervalo, en minutos, para ejecutar el resumen programado de alertas de fraude."
  type        = number
  default     = 7

  validation {
    condition     = var.fraud_alert_summary_interval_minutes >= 1 && var.fraud_alert_summary_interval_minutes <= 1440
    error_message = "fraud_alert_summary_interval_minutes debe estar entre 1 y 1440 minutos."
  }
}

variable "google_oauth_client_id" {
  description = "Client ID de Google OAuth para habilitar el IdP opcional en Cognito. Dejar vacío deshabilita Google."
  type        = string
  default     = ""
}

variable "google_oauth_client_secret" {
  description = "Client secret de Google OAuth para habilitar el IdP opcional en Cognito. Dejar vacío deshabilita Google."
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = (var.google_oauth_client_id == "" && var.google_oauth_client_secret == "") || (var.google_oauth_client_id != "" && var.google_oauth_client_secret != "")
    error_message = "google_oauth_client_id y google_oauth_client_secret deben establecerse juntos o quedar ambos vacíos."
  }
}
