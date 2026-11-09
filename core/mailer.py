"""Отправка писем через SMTP почтового сервиса (Яндекс, Gmail и другие).

Настройки берутся из секретов: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM.
"""
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from core.logging_setup import get_logger, mask_email

log = get_logger("mailer")
TIMEOUT = 20


class MailError(Exception):
    """Письмо не ушло. Текст показывается пользователю."""


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str


class Mailer:
    def __init__(self, settings: SmtpSettings):
        self.settings = settings

    def send(self, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        context = ssl.create_default_context()
        try:
            if self.settings.port == 465:
                with smtplib.SMTP_SSL(self.settings.host, 465, context=context, timeout=TIMEOUT) as server:
                    server.login(self.settings.user, self.settings.password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(self.settings.host, self.settings.port, timeout=TIMEOUT) as server:
                    server.starttls(context=context)
                    server.login(self.settings.user, self.settings.password)
                    server.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            log.error("Письмо на %s не отправлено: %s", mask_email(to), type(error).__name__)
            raise MailError("Письмо не отправилось. Попробуйте через минуту") from error
        log.info("Письмо отправлено на %s", mask_email(to))


def code_letter(code: str) -> tuple[str, str]:
    """Тема и текст письма с кодом подтверждения."""
    return (
        f"Код для входа: {code}",
        f"Ваш код подтверждения почты в Personal Food Parser: {code}\n\n"
        "Код действует 15 минут. Если вы не регистрировались, просто удалите это письмо.",
    )
