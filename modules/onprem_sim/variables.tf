variable "project" {
  description = "Nombre del proyecto, usado como prefijo en los nombres de los recursos provisionados por el módulo."
  type        = string

  validation {
    condition     = length(var.project) > 0 && length(var.project) <= 24
    error_message = "El nombre de proyecto debe tener entre 1 y 24 caracteres para que los nombres derivados respeten los límites de AWS."
  }
}

variable "vpn_gateway_id" {
  description = "ID del Virtual Private Gateway al que se conecta la VPN site-to-site (proviene del módulo network)."
  type        = string

  validation {
    condition     = length(var.vpn_gateway_id) > 0
    error_message = "vpn_gateway_id no puede estar vacío; debe ser un ID válido de Virtual Private Gateway."
  }
}

variable "aws_vpc_cidr" {
  description = "CIDR de la VPC del lado AWS, usado para autorizar tráfico desde la VPN en el Security Group del router on-premise."
  type        = string

  validation {
    condition     = can(cidrhost(var.aws_vpc_cidr, 0))
    error_message = "aws_vpc_cidr debe ser un bloque CIDR IPv4 válido."
  }
}

variable "azs" {
  description = "Lista de Availability Zones disponibles. Sólo se usa la primera para alojar la subnet pública del on-premise simulado."
  type        = list(string)

  validation {
    condition     = length(var.azs) >= 1
    error_message = "Se requiere al menos una Availability Zone para crear la subnet pública del on-premise simulado."
  }
}

variable "onprem_vpc_cidr" {
  description = "Bloque CIDR de la VPC simulada de on-premise. Debe no solaparse con la VPC del lado AWS."
  type        = string
  default     = "192.168.0.0/16"

  validation {
    condition     = can(cidrhost(var.onprem_vpc_cidr, 0))
    error_message = "onprem_vpc_cidr debe ser un bloque CIDR IPv4 válido."
  }
}

variable "onprem_public_subnet_cidr" {
  description = "Bloque CIDR de la subnet pública on-premise donde corre el EC2 strongSwan."
  type        = string
  default     = "192.168.1.0/24"

  validation {
    condition     = can(cidrhost(var.onprem_public_subnet_cidr, 0))
    error_message = "onprem_public_subnet_cidr debe ser un bloque CIDR IPv4 válido."
  }
}

variable "instance_type" {
  description = "Tipo de instancia EC2 para el VPN gateway strongSwan. Restringido a los valores aceptados por la plantilla CloudFormation."
  type        = string
  default     = "t3.micro"

  validation {
    condition     = contains(["t3.micro", "t3.small", "t3.medium"], var.instance_type)
    error_message = "instance_type debe ser uno de t3.micro, t3.small o t3.medium (valores permitidos por templates/vpn-gateway-strongswan.yml)."
  }
}

variable "sqs_vpce_eni_count" {
  description = "Cantidad de ENIs del Interface VPC Endpoint de SQS (debe coincidir con subnets del endpoint). Se usa para for_each/count con índices fijos; los IDs reales pueden ser unknown hasta apply."
  type        = number
  default     = 0

  validation {
    condition     = var.sqs_vpce_eni_count >= 0 && var.sqs_vpce_eni_count <= 8
    error_message = "sqs_vpce_eni_count debe estar entre 0 y 8."
  }
}

variable "sqs_vpc_endpoint_network_interface_ids" {
  description = "IDs de las ENIs del Interface VPC Endpoint de SQS en la VPC AWS. Debe tener al menos sqs_vpce_eni_count elementos cuando ese valor es > 0. El módulo crea PHZ sqs.<region>.amazonaws.com cuando sqs_vpce_eni_count > 0."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.sqs_vpc_endpoint_network_interface_ids : can(regex("^eni-[0-9a-f]+$", id))])
    error_message = "Cada elemento de sqs_vpc_endpoint_network_interface_ids debe tener el formato eni-<hex>."
  }
}

variable "enable_traffic_producers" {
  description = "Crea dos instancias EC2 en la VPC on-premise que envían transacciones sintéticas de forma continua a la cola SQS de ingesta."
  type        = bool
  default     = true
}

variable "ingestion_queue_url" {
  description = "URL de la cola SQS de ingesta de transacciones consumida por los productores on-premise."
  type        = string
  default     = ""

  validation {
    condition     = var.ingestion_queue_url == "" || can(regex("^https://sqs\\.", var.ingestion_queue_url))
    error_message = "ingestion_queue_url debe ser una URL HTTPS de SQS o quedar vacía cuando enable_traffic_producers = false."
  }

  validation {
    condition     = !var.enable_traffic_producers || var.ingestion_queue_url != ""
    error_message = "ingestion_queue_url no puede estar vacía cuando enable_traffic_producers = true."
  }
}

variable "producer_instance_type" {
  description = "Tipo de instancia EC2 para los productores de tráfico on-premise simulado."
  type        = string
  default     = "t3.micro"

  validation {
    condition     = contains(["t3.micro", "t3.small", "t3.medium"], var.producer_instance_type)
    error_message = "producer_instance_type debe ser uno de t3.micro, t3.small o t3.medium."
  }
}

variable "producer_batch_size" {
  description = "Cantidad de mensajes que cada productor intenta enviar por iteración del loop continuo."
  type        = number
  default     = 5

  validation {
    condition     = var.producer_batch_size >= 1 && var.producer_batch_size <= 1000
    error_message = "producer_batch_size debe estar entre 1 y 1000."
  }
}

variable "producer_loop_interval_sec" {
  description = "Segundos de espera entre iteraciones de envío continuo por productor (con 2 productores: ~50 tx/min por defecto)."
  type        = number
  default     = 12

  validation {
    condition     = var.producer_loop_interval_sec >= 1 && var.producer_loop_interval_sec <= 60
    error_message = "producer_loop_interval_sec debe estar entre 1 y 60 segundos."
  }
}

variable "producer_fraud_pct" {
  description = "Porcentaje de transacciones generadas con patrones de fraude por cada productor on-premise."
  type        = number
  default     = 8

  validation {
    condition     = var.producer_fraud_pct >= 0 && var.producer_fraud_pct <= 100
    error_message = "producer_fraud_pct debe estar entre 0 y 100."
  }
}

variable "tags" {
  description = "Tags comunes a propagar a todos los recursos creados por el módulo (mergeados con tags específicos)."
  type        = map(string)
  default     = {}
}
