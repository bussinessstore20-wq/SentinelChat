from dataclasses import dataclass


@dataclass
class MemberProfile:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    has_photo: bool
    is_admin: bool


@dataclass
class ModerationRule:
    require_photo: bool = False
    require_first_name: bool = False
    require_last_name: bool = False
    require_username: bool = False
    ignore_admins: bool = True
    action: str = "review"
    dry_run: bool = True


def analyze_member(
    member: MemberProfile,
    rules: ModerationRule,
) -> list[str]:

    if rules.ignore_admins and member.is_admin:
        return []

    violations: list[str] = []

    if rules.require_photo and not member.has_photo:
        violations.append("missing_photo")

    if rules.require_first_name and not member.first_name:
        violations.append("missing_first_name")

    if rules.require_last_name and not member.last_name:
        violations.append("missing_last_name")

    if rules.require_username and not member.username:
        violations.append("missing_username")

    return violations