from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from .models import AuditLog
from .services import record_system_audit_event


@receiver(user_logged_in, dispatch_uid="recruitguard_internal_login_audit")
def audit_internal_login(sender, request, user, **kwargs):
    if getattr(user, "is_internal_user", False):
        record_system_audit_event(
            actor=user,
            action=AuditLog.Action.INTERNAL_LOGIN,
            description="Internal user logged in.",
            metadata={"user_id": user.id},
        )


@receiver(user_logged_out, dispatch_uid="recruitguard_internal_logout_audit")
def audit_internal_logout(sender, request, user, **kwargs):
    if getattr(user, "is_internal_user", False):
        record_system_audit_event(
            actor=user,
            action=AuditLog.Action.INTERNAL_LOGOUT,
            description="Internal user logged out.",
            metadata={"user_id": user.id},
        )
