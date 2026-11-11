"""Connection profiles: persistence, credentials, testing and discovery."""

from __future__ import annotations

from dataclasses import dataclass, field

from dbnero.adapters.base import DatabaseAdapter, create_adapter
from dbnero.adapters.tools import ENGINE_TOOLS, ToolInfo, check_engine_tools, list_odbc_drivers
from dbnero.core.logging_setup import masking_filter
from dbnero.core.models import AuthType, ConnectionProfile, Engine
from dbnero.core.settings import Settings
from dbnero.security.credentials import CredentialStore
from dbnero.storage.catalog import Catalog


class ValidationError(ValueError):
    pass


@dataclass(slots=True)
class EngineToolReport:
    engine: Engine
    tools: list[ToolInfo] = field(default_factory=list)
    odbc_drivers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.engine is Engine.MSSQL:
            return bool(self.odbc_drivers)
        return all(t.available for t in self.tools)

    @property
    def missing(self) -> list[str]:
        if self.engine is Engine.MSSQL:
            return [] if self.odbc_drivers else ["ODBC Driver for SQL Server"]
        return [t.name for t in self.tools if not t.available]


INSTALL_HINTS: dict[Engine, dict[str, str]] = {
    Engine.POSTGRESQL: {
        "windows": "PostgreSQL kurulum paketindeki 'Command Line Tools' bileşenini kurun "
        "(https://www.postgresql.org/download/windows/) ve bin klasörünü PATH'e ekleyin "
        "veya Ayarlar'dan yolunu gösterin.",
        "darwin": "brew install libpq && brew link --force libpq",
        "linux": "Debian/Ubuntu: sudo apt install postgresql-client; "
        "RHEL/Fedora: sudo dnf install postgresql",
    },
    Engine.MYSQL: {
        "windows": "MySQL Installer ile 'MySQL Shell/Client Programs' kurun veya "
        "MySQL Server ZIP paketindeki bin klasörünü Ayarlar'dan gösterin.",
        "darwin": "brew install mysql-client",
        "linux": "Debian/Ubuntu: sudo apt install mysql-client; "
        "RHEL/Fedora: sudo dnf install mysql",
    },
    Engine.MSSQL: {
        "windows": "Microsoft ODBC Driver 18 for SQL Server'ı kurun "
        "(https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).",
        "darwin": "brew tap microsoft/mssql-release && "
        "HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18",
        "linux": "https://learn.microsoft.com/sql/connect/odbc/linux-mac/"
        "installing-the-microsoft-odbc-driver-for-sql-server adımlarıyla msodbcsql18 kurun.",
    },
}


def install_hint(engine: Engine, platform: str) -> str:
    key = "windows" if platform.startswith("win") else "darwin" if platform == "darwin" else "linux"
    return INSTALL_HINTS[engine][key]


class ConnectionService:
    def __init__(self, catalog: Catalog, credentials: CredentialStore, settings: Settings) -> None:
        self.catalog = catalog
        self.credentials = credentials
        self.settings = settings

    # -- persistence ------------------------------------------------------

    def list_profiles(self) -> list[ConnectionProfile]:
        return self.catalog.list_connections()

    def get(self, connection_id: str) -> ConnectionProfile:
        return self.catalog.get_connection(connection_id)

    @staticmethod
    def validate(profile: ConnectionProfile) -> None:
        if not profile.name.strip():
            raise ValidationError("Bağlantı adı boş olamaz")
        if not profile.host.strip():
            raise ValidationError("Sunucu adresi boş olamaz")
        if not (0 < profile.port < 65536):
            raise ValidationError("Port 1-65535 aralığında olmalı")
        if profile.auth_type is AuthType.WINDOWS and profile.engine is not Engine.MSSQL:
            raise ValidationError("Windows kimlik doğrulaması yalnızca SQL Server için geçerlidir")
        if profile.auth_type is AuthType.PASSWORD and not profile.username.strip():
            raise ValidationError("Kullanıcı adı boş olamaz")

    def save(self, profile: ConnectionProfile, password: str | None = None) -> None:
        """Save a profile. ``password=None`` keeps the stored password; ``""`` clears it."""
        self.validate(profile)
        if any(
            c.name.casefold() == profile.name.casefold() and c.id != profile.id
            for c in self.catalog.list_connections()
        ):
            raise ValidationError(f"'{profile.name}' adında bir bağlantı zaten var")
        self.catalog.save_connection(profile)
        if password is not None:
            if password:
                self.credentials.set_password(profile.id, password)
            else:
                self.credentials.delete_password(profile.id)

    def delete(self, connection_id: str) -> None:
        self.catalog.delete_connection(connection_id)
        self.credentials.delete_password(connection_id)

    def has_password(self, connection_id: str) -> bool:
        return bool(self.credentials.get_password(connection_id))

    # -- adapters ---------------------------------------------------------

    def adapter_for(
        self, profile: ConnectionProfile | str, password: str | None = None
    ) -> DatabaseAdapter:
        if isinstance(profile, str):
            profile = self.get(profile)
        if password is None and profile.auth_type is AuthType.PASSWORD:
            password = self.credentials.get_password(profile.id)
        if password:
            masking_filter.register(password)
        return create_adapter(profile, password, self.settings.tool_paths)

    def test(self, profile: ConnectionProfile, password: str | None = None) -> str:
        self.validate(profile)
        return self.adapter_for(profile, password).test_connection()

    def list_databases(self, connection_id: str, include_system: bool = False) -> list[str]:
        return self.adapter_for(connection_id).list_databases(include_system)

    # -- tooling ----------------------------------------------------------

    def check_tools(self, engine: Engine) -> EngineToolReport:
        report = EngineToolReport(engine)
        if ENGINE_TOOLS[engine]:
            report.tools = check_engine_tools(engine, self.settings.tool_paths)
        if engine is Engine.MSSQL:
            report.odbc_drivers = list_odbc_drivers()
        return report

    def check_all_tools(self) -> list[EngineToolReport]:
        return [self.check_tools(e) for e in Engine]
