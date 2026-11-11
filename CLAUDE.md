# CLAUDE.md

Bu dosya, bu depoda çalışan Claude Code (ve diğer yapay zekâ asistanları) için proje bağlamı ve çalışma kurallarını içerir.

## Proje özeti

**dbnero**, MSSQL, MySQL ve PostgreSQL için çapraz platform (Windows/macOS/Linux) bir **masaüstü yedekleme, geri yükleme ve karşılaştırma** uygulamasıdır.

- Dil: **Python 3.12+**
- Arayüz: **PySide6**
- Katalog: **SQLite + SQLAlchemy**
- Parolalar: **keyring** (OS anahtar kasası)
- Paket adı: `dbnero` (`src/dbnero/` altında, src-layout)

Görev listesi ve ilerleme: `TASKS.md`. Kullanıcıya yönelik açıklama: `README.md`.

## Dil kuralları

- Kod, değişken/fonksiyon/sınıf isimleri, docstring'ler ve kod yorumları: **İngilizce**
- Kullanıcıya görünen arayüz metinleri: Türkçe (ileride i18n için `self.tr()` / Qt çeviri altyapısı kullanılmalı)
- Dokümantasyon (`README.md`, `TASKS.md`, `docs/`): Türkçe
- Commit mesajları: İngilizce, kısa ve emir kipinde (ör. `Add PostgreSQL backup adapter`)

## Mimari

```
ui/  ──►  core/ (services)  ──►  adapters/ (engine-specific)
                           └──►  storage/ (catalog, manifests, archive)
                           └──►  compare/ (schema_diff, data_diff, snapshots)
security/credentials.py  ← keyring wrapper, used by core/adapters
```

Katman kuralları:
- `ui/` yalnızca `core/` servislerini çağırır; **asla doğrudan adaptör, SQL veya subprocess çalıştırmaz**.
- `core/` Qt'ye bağımlı olmamalıdır (UI'sız test edilebilir olmalı). Qt sinyallerine köprü `ui/` veya ince bir `jobs` katmanında olur.
- `adapters/` motor bağımlı her şeyi içerir; diğer katmanlar motor adına göre `if/else` yazmaz.
- `compare/` normalize edilmiş, motordan bağımsız `SchemaSnapshot` modeli üzerinde çalışır.
- Uzun süren işlemler (yedek, restore, diff) UI thread'inde **asla** çalışmaz; `QThreadPool`/iş kuyruğu kullanılır, ilerleme ve iptal desteklenir.

### Adaptör sözleşmesi

```python
class DatabaseAdapter(Protocol):          # src/dbnero/adapters/base.py
    profile: ConnectionProfile
    def test_connection(self) -> str: ...  # returns server version, raises ConnectionFailed
    def list_databases(self, include_system: bool = False) -> list[str]: ...
    def backup(self, database: str, destination: Path, options: dict, ctx: JobContext | None = None) -> BackupResult: ...
    def restore(self, source: Path, target_database: str, options: dict, ctx: JobContext | None = None) -> RestoreResult: ...
    def inspect_schema(self, database: str) -> SchemaSnapshot: ...
    def validate_backup(self, source: Path) -> ValidationResult: ...
```

- Adaptörler `create_adapter(profile, password)` ile oluşturulur; motor → fabrika eşlemesi `register_adapter` ile yapılır.
- `JobContext`: iptal bayrağı (`raise_if_cancelled`) + ilerleme geri çağrısı (`progress`).
- Harici araçlar **yalnızca** `adapters/process.run_tool` ile çalıştırılır; parola dosyaları `process.secret_file` ile.
- Dinamik SQL'de isimler `adapters/identifiers.quote_identifier` ile kaçışlanır.

Yeni motor eklemek = `adapters/` altında bu protokolü uygulamak + entegrasyon testleri.

## Motor bazlı yöntemler (değiştirmeden önce oku)

- **PostgreSQL:** `pg_dump --format=custom` → `.dump`, geri yükleme `pg_restore`. Roller/global nesneler dahil değil.
- **MySQL:** `mysqldump` → `.sql`/`.sql.gz`, InnoDB için `--single-transaction`; geri yükleme `mysql` istemcisi.
- **MSSQL:** T-SQL `BACKUP DATABASE` / `RESTORE DATABASE` (+ `WITH MOVE`). `.bak` **SQL Server sunucusunun** dosya sistemine yazılır, uygulamanın çalıştığı makineye değil. Yerel arşive alma ayrı bir aktarım adımıdır.

## Kritik iş kuralları (ihlal etme)

1. **Toplu yedekte her veritabanı ayrı dosya + ayrı katalog kaydı** üretir. Bir alt işin başarısızlığı diğerlerini iptal etmez veya gizlemez.
2. `created_at_utc` sistem tarafından atanır ve **değiştirilemez**. Kullanıcı yalnızca ayrı, isteğe bağlı bir "referans tarihi" girebilir.
3. Geri yükleme yalnızca **aynı motor içinde**. Motorlar arası taşıma kapsam dışıdır.
4. Var olan veritabanının üzerine yazma **varsayılan olarak kapalı**; açık kullanıcı onayı gerekir.
5. Geri yükleme bir transaction gibi geri alınabilir **varsayılmaz**; UI riskleri açıkça bildirir.
6. Yedek dosyaları önce geçici adla yazılır, başarı + checksum sonrası **atomik olarak** son adına taşınır. Yarım dosya asla "başarılı" görünmez.
7. Her yedek için SHA-256 hesaplanır ve `manifest.json` yedeğin yanına yazılır; katalog kaybolursa arşiv manifest'lerden yeniden içe aktarılabilmelidir.

## Güvenlik kuralları

- Parolalar ve sırlar **yalnızca** `security/credentials.py` (keyring) üzerinden. SQLite kataloğuna, manifest'e, loglara veya **komut satırı argümanlarına** yazılmaz.
  - `pg_dump`/`pg_restore` için `PGPASSWORD` ortam değişkeni veya geçici `.pgpass` (0600).
  - `mysqldump`/`mysql` için geçici `--defaults-extra-file` (0600) — `-p<parola>` argümanı **kullanma**.
- `subprocess` çağrılarında **`shell=True` kullanma**; argümanları liste olarak ver.
- Dinamik SQL'de nesne isimlerini motora özgü şekilde doğrula/kaçışla (`QUOTENAME`, `"..."`, `` `...` ``); değerler için her zaman parametre bağlama.
- Log ve hata mesajlarında bağlantı dizelerindeki parolaları maskele.
- Karşılaştırma işlemleri mümkün olduğunca salt-okuma yetkisiyle yapılır; rapor dışa aktarımında hassas veri uyarısı göster.

## Katalog meta verileri (backups tablosu)

`backup_id` (UUID), `batch_id`, `connection_id`, `engine`, `engine_version`, `database_name`, `created_at_utc`, `description`, `tags`, `backup_format`, `backup_mode`, `file_path`, `file_size`, `checksum_sha256`, `status`, `tool_versions`, `schema_snapshot_path`, `comparison_snapshot_path`.

Şema değişiklikleri `storage/migrations.py` içindeki sıralı listeye **yeni bir migration eklenerek** yapılır (`PRAGMA user_version`). Yayınlanmış migration düzenlenmez. Yeni sütun eklersen `storage/catalog.py` içindeki `Table` tanımını da güncelle. `created_at_utc` bir SQLite trigger'ı ile de korunur.

## Dosya ve isimlendirme düzeni

```
<arşiv_kökü>/<motor>/<sunucu>/<veritabanı>/<yıl>/<ay>/
    <YYYY-MM-DD_HHMMSS>_<motor>_<veritabanı>_<kısa_id>.<uzantı>
    <aynı_ad>.manifest.json
```

Zamanlar UTC saklanır, arayüzde kullanıcının yerel saat diliminde gösterilir. Dosya adlarındaki sunucu/veritabanı adları dosya sistemi için güvenli hale getirilir (sanitize).

## Komutlar

```bash
pip install -e ".[dev]"     # geliştirme bağımlılıkları
python -m dbnero            # uygulamayı başlat
pytest tests/unit           # birim testleri (hızlı, harici DB gerektirmez)
pytest tests/integration    # Docker'daki gerçek MSSQL/MySQL/PostgreSQL ile
ruff check . && ruff format --check .
mypy src
```

Linux'ta Qt testleri için sistem kütüphaneleri gerekir: `libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3`. Başsız ortamda `QT_QPA_PLATFORM=offscreen` kullan (`tests/conftest.py` bunu otomatik ayarlar).

Commit öncesi: `ruff check . && ruff format --check . && mypy && pytest` temiz olmalı.

## Test kuralları

- Her yeni servis/adaptör fonksiyonu için birim testi. Adaptörlerde `subprocess` ve DB sürücüleri mock'lanır.
- Yedekle → doğrula → geri yükle → şema karşılaştır döngüsü her motor için entegrasyon testiyle doğrulanır (`tests/integration`, Docker Compose).
- UI testleri için `pytest-qt`.
- Bir değişikliği "tamam" saymadan önce ilgili testleri çalıştır.

## Kod konumları (hızlı başvuru)

| Konu | Dosya |
|---|---|
| Domain modelleri, enum'lar | `src/dbnero/core/models.py` |
| Ayarlar (platformdirs, JSON) | `src/dbnero/core/settings.py` |
| Loglama + parola maskeleme | `src/dbnero/core/logging_setup.py` |
| Katalog / migration | `src/dbnero/storage/catalog.py`, `storage/migrations.py` |
| Arşiv yolları, atomik yazma, SHA-256 | `src/dbnero/storage/archive.py` |
| Manifest okuma/yazma | `src/dbnero/storage/manifests.py` |
| Katalogu arşivden yeniden kurma | `src/dbnero/core/catalog_service.py` |
| Adaptör sözleşmesi, registry | `src/dbnero/adapters/base.py` |
| Alt süreç çalıştırma | `src/dbnero/adapters/process.py` |
| Araç keşfi | `src/dbnero/adapters/tools.py` |
| Şema anlık görüntüsü modeli | `src/dbnero/compare/snapshots.py` |
| Keyring | `src/dbnero/security/credentials.py` |
| Ana pencere | `src/dbnero/ui/main_window.py` |

## Kapsam dışı (MVP)

Motorlar arası taşıma, büyük ölçekli satır bazında veri diff'i, otomatik izole restore ortamı, zamanlanmış yedekleme, bulut depolama, şifreli arşiv, ekip/merkezi yönetim. Bunlar için kod yazmadan önce kullanıcıya sor.

## Çalışma şekli

- Görevleri `TASKS.md` üzerinden takip et; tamamlanan maddeyi `[x]` olarak işaretle.
- Küçük, odaklı commit'ler at.
- Mimari kararlar değişirse `docs/architecture.md` ve bu dosyayı güncelle.
