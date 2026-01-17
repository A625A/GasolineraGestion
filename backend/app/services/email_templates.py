from __future__ import annotations

from typing import Any, Dict, Literal, TypedDict

NotificationTemplate = Literal[
  "gas_price_drop",
  "gas_price_drop_forecast",
  "product_expiration",
  "payment_reminder",
  "general_notice",
]

Locale = Literal["es", "en"]


class EmailContent(TypedDict):
  subject: str
  body: str


class TemplateDefinition(TypedDict):
  required_fields: set[str]
  es: EmailContent
  en: EmailContent


class TemplateValidationError(ValueError):
  """Raised when a notification template cannot be rendered."""


def _stringify(value: Any) -> str:
  if value is None:
    return ""
  return str(value)


def _stringify_mapping(values: Dict[str, Any]) -> Dict[str, str]:
  return {key: _stringify(value) for key, value in values.items()}


EMAIL_TEMPLATES: Dict[NotificationTemplate, TemplateDefinition] = {
  "gas_price_drop": {
    "required_fields": {"nombre", "precio_actual", "precio_anterior"},
    "es": {
      "subject": "¡Buenas noticias! Bajó el precio de la gasolina ⛽",
      "body": (
        "Hola {nombre},\n"
        "Queremos informarte que el precio de la gasolina ha disminuido recientemente.\n"
        "Aprovecha para llenar tu tanque y ahorrar en tu próxima visita.\n"
        "Precio actual: {precio_actual}\n"
        "Antes: {precio_anterior}\n\n"
        "¡Te esperamos en nuestra estación!"
      ),
    },
    "en": {
      "subject": "Good news! Gasoline price dropped ⛽",
      "body": (
        "Hi {nombre},\n"
        "We wanted to let you know that gasoline prices have recently gone down.\n"
        "This is a great time to fill up and save on your next visit.\n"
        "Current price: {precio_actual}\n"
        "Previous price: {precio_anterior}\n\n"
        "See you at the station!"
      ),
    },
  },
  "gas_price_drop_forecast": {
    "required_fields": {"nombre", "fecha_estimada"},
    "es": {
      "subject": "Anticípate: el precio de la gasolina bajará pronto ⛽",
      "body": (
        "Hola {nombre},\n"
        "Según nuestras previsiones, el precio de la gasolina bajará el {fecha_estimada}.\n"
        "Si planeas repostar, te recomendamos esperar para aprovechar el mejor precio.\n\n"
        "Te mantendremos informado."
      ),
    },
    "en": {
      "subject": "Heads up: fuel prices will drop soon ⛽",
      "body": (
        "Hi {nombre},\n"
        "Based on our forecasts, the gasoline price will go down on {fecha_estimada}.\n"
        "If you plan to refuel, consider waiting a little to secure the best rate.\n\n"
        "We will keep you posted."
      ),
    },
  },
  "product_expiration": {
    "required_fields": {"nombre", "nombre_producto", "fecha_vencimiento"},
    "es": {
      "subject": "Recordatorio: producto por vencer pronto ⚠️",
      "body": (
        "Hola {nombre},\n"
        "El siguiente producto está próximo a vencer:\n"
        "{nombre_producto} – Fecha de vencimiento: {fecha_vencimiento}\n\n"
        "Revisa tu inventario y considera reponerlo o realizar una oferta antes de esa fecha."
      ),
    },
    "en": {
      "subject": "Reminder: product is expiring soon ⚠️",
      "body": (
        "Hi {nombre},\n"
        "The following product is close to its expiration date:\n"
        "{nombre_producto} – Expiration date: {fecha_vencimiento}\n\n"
        "Review your inventory and consider restocking or discounting it before that day."
      ),
    },
  },
  "payment_reminder": {
    "required_fields": {"nombre", "concepto", "fecha_pago", "monto", "metodo_pago"},
    "es": {
      "subject": "Recordatorio de pago pendiente 💰",
      "body": (
        "Hola {nombre},\n"
        "Te recordamos que tienes un pago pendiente por {concepto} con fecha límite {fecha_pago}.\n\n"
        "Monto: {monto}\n"
        "Método de pago: {metodo_pago}\n\n"
        "Por favor, realiza el pago a tiempo para evitar recargos o interrupciones en el servicio."
      ),
    },
    "en": {
      "subject": "Payment reminder 💰",
      "body": (
        "Hi {nombre},\n"
        "This is a reminder that you have a pending payment for {concepto} due on {fecha_pago}.\n\n"
        "Amount: {monto}\n"
        "Payment method: {metodo_pago}\n\n"
        "Please complete the payment on time to avoid fees or service interruptions."
      ),
    },
  },
  "general_notice": {
    "required_fields": {"nombre", "detalle_notificacion", "link_panel"},
    "es": {
      "subject": "Nueva notificación de tu cuenta 📩",
      "body": (
        "Hola {nombre},\n"
        "Tienes una nueva notificación en tu cuenta:\n"
        "{detalle_notificacion}\n\n"
        "Accede a tu panel para más información: {link_panel}\n\n"
        "Gracias por usar nuestra plataforma."
      ),
    },
    "en": {
      "subject": "You have a new account notification 📩",
      "body": (
        "Hi {nombre},\n"
        "There is a new notification in your account:\n"
        "{detalle_notificacion}\n\n"
        "Sign in to your dashboard for more details: {link_panel}\n\n"
        "Thank you for using our platform."
      ),
    },
  },
}


def available_templates() -> list[NotificationTemplate]:
  return list(EMAIL_TEMPLATES.keys())


def render_notification_email(
  template: NotificationTemplate,
  data: Dict[str, Any],
  *,
  locale: Locale = "es",
  bilingual: bool = True,
) -> tuple[str, str]:
  definition = EMAIL_TEMPLATES.get(template)
  if not definition:
    raise TemplateValidationError(f"Unknown template '{template}'.")

  required_fields = definition["required_fields"]
  missing = [field for field in required_fields if field not in data]
  if missing:
    raise TemplateValidationError(f"Missing required fields for template '{template}': {', '.join(sorted(missing))}.")

  mapping = _stringify_mapping(data)

  if locale not in ("es", "en"):
    locale = "es"

  try:
    primary_content = definition[locale]
    subject = primary_content["subject"].format_map(mapping)
    body = primary_content["body"].format_map(mapping)
  except KeyError as exc:
    raise TemplateValidationError(f"Missing placeholder value: {exc.args[0]}.") from exc

  if bilingual:
    secondary_locale: Locale = "en" if locale == "es" else "es"
    secondary_content = definition.get(secondary_locale)
    if secondary_content:
      secondary_label = "--- English version ---" if secondary_locale == "en" else "--- Versión en español ---"
      try:
        secondary_body = secondary_content["body"].format_map(mapping)
      except KeyError as exc:  # pragma: no cover - defensive branch
        raise TemplateValidationError(f"Missing placeholder value: {exc.args[0]}.") from exc
      body = f"{body}\n\n{secondary_label}\n{secondary_body}"

  return subject, body
