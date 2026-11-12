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
- Kullanıcıya görünen arayüz metinleri ve servislerin ürettiği log/hata mesajları: **İngilizce** (uygulama tamamen İngilizce)
- `README.md` ve `site/` (indirme sayfası): İngilizce. `TASKS.md`, `CLAUDE.md`, `docs/`: Türkçe
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
class DatabaseAdapter(Protocol):  # src/dbnero/adapters/base.py
    profile: ConnectionProfile

    def test_connection(self) -> str: ...  # returns server version, raises ConnectionFailed
    def list_databases(self, include_system: bool = False) -> list[str]: ...
    def backup(
        self, database: str, destination: Path, options: dict, ctx: JobContext | None = None
    ) -> BackupResult: ...
    def restore(
        self, source: Path, target_database: str, options: dict, ctx: JobContext | None = None
    ) -> RestoreResult: ...
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
4. Geri yükleme tek bir hedef veritabanı adı alır (varsayılan: yedeğin kendi adı). Hedef varsa üzerine yazılır, ancak yalnızca kullanıcı "Yes, replace this database" onayını işaretledikten sonra; onaysız var olan veritabanına asla yazılmaz.
5. Geri yükleme bir transaction gibi geri alınabilir **varsayılmaz**; UI riskleri açıkça bildirir.
6. Yedek dosyaları önce geçici adla yazılır, başarı + checksum sonrası **atomik olarak** son adına taşınır. Yarım dosya asla "başarılı" görünmez.
7. Her yedek için SHA-256 hesaplanır ve `manifest.json` yedeğin yanına yazılır; katalog kaybolursa arşiv manifest'lerden yeniden içe aktarılabilmelidir.

## Güvenlik kuralları

- Parolalar ve sırlar **yalnızca** `security/credentials.py` (keyring / Windows DPAPI dosyası) üzerinden. SQLite kataloğuna, manifest'e, loglara veya **komut satırı argümanlarına** yazılmaz.
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
pytest tests/integration    # Docker'daki gerçek MSSQL/MySQL/PostgreSQL ile (önce: mkdir -p .mssql-share && chmod 777 .mssql-share && docker compose -f docker-compose.test.yml up -d --wait)
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
| Servislerin kurulumu (composition root) | `src/dbnero/core/app.py` (`AppContext`) |
| Bağlantılar, araç kontrolü, kurulum ipuçları | `src/dbnero/core/connection_service.py` |
| Tekil/toplu yedek, doğrulama, silme, saklama politikası | `src/dbnero/core/backup_service.py` |
| Geri yükleme (ön kontrol, onay, koruyucu yedek, doğrulama) | `src/dbnero/core/restore_service.py` |
| Şema/veri karşılaştırma, izole geçici restore | `src/dbnero/core/compare_service.py` |
| Arka plan işleri (iptal, ilerleme, kalıcı log) | `src/dbnero/core/jobs.py` |
| Genel bakış istatistikleri, arşivden içe aktarma | `src/dbnero/core/catalog_service.py` |
| Ayarlar (platformdirs, JSON) | `src/dbnero/core/settings.py` |
| Loglama + parola maskeleme | `src/dbnero/core/logging_setup.py` |
| Katalog / migration | `src/dbnero/storage/catalog.py`, `storage/migrations.py` |
| Arşiv yolları, atomik yazma, SHA-256 | `src/dbnero/storage/archive.py` |
| Manifest okuma/yazma | `src/dbnero/storage/manifests.py` |
| Adaptör sözleşmesi, registry | `src/dbnero/adapters/base.py` |
| Motor adaptörleri | `src/dbnero/adapters/{postgresql,mysql,mssql}.py` |
| Alt süreç çalıştırma, gizli dosyalar | `src/dbnero/adapters/process.py` |
| Araç keşfi | `src/dbnero/adapters/tools.py` |
| Şema modeli / şema diff / veri diff / rapor | `src/dbnero/compare/{snapshots,schema_diff,data_diff,report}.py` |
| Keyring (+ oturumluk geri dönüş) | `src/dbnero/security/credentials.py` |
| Ana pencere ve ekranlar | `src/dbnero/ui/main_window.py`, `ui/*_page.py` |
| UI yardımcıları (`run_async`, `JobBridge`, biçimlendirme) | `src/dbnero/ui/common.py` |
| Tema token'ları, stil sayfası | `src/dbnero/ui/theme.py` |
| Çizilmiş ikonlar, uygulama ikonu | `src/dbnero/ui/icons.py`, `src/dbnero/ui/assets/dbnero.svg` |
| Paketleme | `packaging/dbnero.spec`, `packaging/windows/dbnero.iss`, `packaging/linux/` |
| Sürüm yayını (installer'lar + GitHub Release) | `.github/workflows/release.yml` (bir `v*` etiketi gönderince çalışır) |
| İndirme sitesi (GitHub Pages) | `site/index.html`, `.github/workflows/pages.yml` |
| Test veritabanları | `docker-compose.test.yml`, `tests/integration/` |

## UI kuralları

- Ağ/disk işi yapan her çağrı UI thread'i dışında çalışır: kısa çağrılar `ui.common.run_async`, uzun işler `app.jobs.submit(...)` + `JobBridge.when_done`.
- Qt combobox'ları `StrEnum` değerlerini düz `str` olarak döndürür; `currentData()` sonucunu her zaman `Engine(...)`, `AuthType(...)` vb. ile dönüştür.
- Tüm kullanıcıya görünen metinler İngilizce, kısa ve eylem odaklı (ör. "Back up 2 databases", "Restore finished").
- Renkler yalnızca `ui/theme.py` token'larından gelir; sayfalarda sabit renk yazma. Açık ve koyu tema ayrı tasarlanmıştır, ikisini de kontrol et.
- Ortak bileşenleri kullan: `Page` (başlık + sağ üst eylemler `header_actions`), `Card`, `Banner`, `Collapsible` (gelişmiş seçenekler), `StatusDelegate` (durum rozeti), `JobProgress`, `button(text, kind)` (`primary` / `danger` / `ghost`).
- Her sayfada tek bir birincil (primary) eylem olsun; nadir kullanılan ayarlar "Advanced options" altında gizli kalsın.
- `QWidget.actions()` ile çakışmaması için sayfa başlığındaki buton düzeni `header_actions` adını kullanır.
- Testler sahte adaptörle (`tests/unit/fakes.py`, `tests/unit/conftest.py::world`) çalışır; yeni ekran davranışı için `tests/unit/test_main_window.py`'ye test ekle.

## Parolalar

`AppContext` varsayılan olarak `default_credential_store()` kullanır: önce OS anahtar kasası; Windows'ta kasa erişimi reddederse (ör. hata 1312) yapılandırma klasöründeki `credentials.dat` dosyasına **DPAPI ile şifrelenmiş** olarak yazar (yalnızca aynı Windows kullanıcısı çözebilir). Hiçbiri yoksa parola **yalnızca bellekte, oturum boyunca** tutulur ve `credential_store_available()` False döner (UI uyarı gösterir). Bağlantı formundaki "Save password" işareti kaldırılırsa parola `set_session_password` ile yalnızca oturumluk tutulur ve kayıtlı kopya silinir. Parolayı **şifresiz** diske yazan bir geri dönüş ekleme.

## Sürüm yayınlama

1. `src/dbnero/__init__.py` içindeki `__version__` değerini artır (pyproject sürümü buradan okunur).
2. Varsayılan branch'e gönder. Release iş akışı `vX.Y.Z` etiketi henüz yoksa etiketi ve GitHub Release'i kendisi oluşturur; Windows installer/portable, macOS `.dmg`, Linux AppImage/`.tar.gz` ve `SHA256SUMS.txt` dosyalarını yükler. (Elle `vX.Y.Z` etiketi göndermek de çalışır.)
3. İndirme bağlantıları sabit dosya adlarıyla `releases/latest/download/<dosya>` adresini kullanır; dosya adlarını değiştirirsen `site/index.html` ve `README.md`'yi de güncelle.

## Kapsam dışı (MVP)

Motorlar arası taşıma, büyük ölçekli satır bazında veri diff'i, otomatik izole restore ortamı, zamanlanmış yedekleme, bulut depolama, şifreli arşiv, ekip/merkezi yönetim. Bunlar için kod yazmadan önce kullanıcıya sor.

## Çalışma şekli

- Görevleri `TASKS.md` üzerinden takip et; tamamlanan maddeyi `[x]` olarak işaretle.
- Küçük, odaklı commit'ler at.
- Mimari kararlar değişirse `docs/architecture.md` ve bu dosyayı güncelle.
