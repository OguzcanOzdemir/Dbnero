# Mimari

Bu belge dbnero'nun katmanlarını ve aralarındaki sözleşmeleri özetler. Proje kuralları için `CLAUDE.md`'ye bakın.

## Katmanlar

| Paket | Sorumluluk | Qt bağımlılığı |
|---|---|---|
| `dbnero.ui` | PySide6 ekranları; yalnızca `core` servislerini çağırır | Var |
| `dbnero.core` | Domain modelleri (`models.py`), servisler, ayarlar, loglama | Yok |
| `dbnero.adapters` | Motor bağımlı her şey: sürücüler, CLI araçları, SQL lehçeleri | Yok |
| `dbnero.compare` | Normalize `SchemaSnapshot` modeli ve diff motorları | Yok |
| `dbnero.storage` | SQLite katalog, arşiv düzeni, manifest dosyaları | Yok |
| `dbnero.security` | OS anahtar kasası (`keyring`) sarmalayıcısı | Yok |

## Adaptör sözleşmesi

`adapters/base.py` içindeki `DatabaseAdapter` protokolü. Adaptörler `ConnectionProfile` ve keyring'den okunan parola ile oluşturulur (`create_adapter`). Uzun süren işlemler (`backup`, `restore`) bir `JobContext` alır: iptal bayrağı + ilerleme geri çağrısı.

Harici araçlar her zaman `adapters/process.run_tool` ile çalıştırılır:

- `shell=False`, argümanlar liste halinde
- parola argv'ye değil ortam değişkenine (`PGPASSWORD`) veya 0600 izinli geçici dosyaya (`secret_file`) yazılır
- iptalde alt süreç sonlandırılır; stderr sınırlı biçimde yakalanır

## Katalog

- SQLite + SQLAlchemy Core (`storage/catalog.py`)
- Şema sürümü `PRAGMA user_version` ile izlenir; `storage/migrations.py` içindeki sıralı migration listesi uygulanır. Yayınlanmış bir migration değiştirilmez, yenisi eklenir. Uygulamadan yeni bir katalog açılırsa `CatalogTooNew` hatası verilir.
- Migration'lar tek transaction içinde çalışır (SQLite DDL transactional).
- Zaman damgaları ISO-8601 UTC metin olarak saklanır; naive `datetime` reddedilir.
- `backups.created_at_utc` hem servis katmanında (`update_backup` izin listesi) hem de veritabanında (trigger) değiştirilemez.
- Parola için hiçbir sütun yoktur.

## Arşiv

```
<kök>/<motor>/<sunucu>/<veritabanı>/<YYYY>/<MM>/
    <YYYY-MM-DD_HHMMSS>_<motor>_<veritabanı>_<kısa_id>.<uzantı>
    <aynı_ad>.manifest.json
    <aynı_ad>.schema.json
```

1. Yedek `<ad>.partial` olarak yazılır (`partial_file`), hata olursa silinir.
2. Araç başarıyla bitince SHA-256 hesaplanır.
3. `finalize` dosyayı atomik olarak son adına taşır (var olan dosyanın üzerine asla yazmaz).
4. `manifest.json` yazılır; içinde sır yoktur, dosya adı göreli tutulur (arşiv taşınabilir).

Katalog kaybolursa `CatalogService.import_from_archive` manifest'lerden kayıtları geri yükler.

## Servisler ve işler

`core/app.py` içindeki `AppContext` tüm servisleri kurar:

| Servis | Görev |
|---|---|
| `ConnectionService` | Profil kaydı (parola keyring'e), adaptör oluşturma, bağlantı testi, araç kontrolü |
| `BackupService` | Tekil/toplu yedek, disk kontrolü, şema görüntüsü, manifest, doğrulama, silme, saklama politikası |
| `RestoreService` | Ön kontroller (`PreflightReport`), ad yazarak onay, koruyucu yedek, şema doğrulaması |
| `CompareService` | Şema diff (snapshot/canlı), veri diff (canlı veya `dbnero_tmp_*` geçici restore) |
| `CatalogService` | Genel bakış istatistikleri, yarım dosya temizliği, manifest'lerden içe aktarma |
| `JobManager` | Thread pool'da iş çalıştırma, `JobContext` ile iptal/ilerleme, logların kataloğa yazılması, çökmeden kalan işlerin işaretlenmesi |

Yedek akışı (`BackupService.backup_database`):

1. Katalogda `running` kaydı açılır (gerçek `created_at_utc` burada atanır).
2. Disk alanı kontrol edilir (veritabanı boyutu + ayarlanan rezerv).
3. Şema anlık görüntüsü alınır (`*.schema.json`). Başarısız olursa uyarı yazılır, yedek devam eder.
4. Adaptör `.partial` dosyasına yazar; SHA-256 hesaplanır; dosya atomik olarak son adına taşınır.
5. Motorun derin doğrulaması çalışır: geçerse `succeeded`, geçmezse `unverified`.
6. Manifest yazılır ve katalog güncellenir. Hata veya iptalde kayıt `failed`/`cancelled` olur, yarım dosyalar silinir.

## Arayüz

- `ui/main_window.py` sekiz ekranı (`*_page.py`) bir yan menüyle bağlar ve ekranlar arası geçişleri yönetir (Arşiv → Geri Yükle/Karşılaştır).
- `ui/common.JobBridge`, `JobManager` olaylarını Qt sinyallerine çevirir (işçi thread → GUI thread).
- `ui/common.run_async`, bağlantı testi gibi kısa çağrıları `QThreadPool` üzerinde çalıştırır.

## Testler

| Katman | Nerede | Not |
|---|---|---|
| Birim | `tests/unit` | Sahte adaptör (`fakes.py`) ile servisler ve UI (`pytest-qt`); adaptörlerde `run_tool` mock'lanır |
| Entegrasyon | `tests/integration` | `docker-compose.test.yml` ile gerçek sunucular; CI'da PostgreSQL 14/16 ve MySQL 8.0/8.4 matrisi |

## Zamanlanmış yedekler

`core/schedule_service.py` bir zamanlamanın ne zaman çalışacağını (`next_run`), çalıştırmayı (`run`/`run_due`) ve "son N yedeği tut" saklamasını yönetir. Çalıştırıcılar:

1. Açık uygulama dakikada bir kontrol eder (`MainWindow.run_due_schedules`, iş kuyruğunda).
2. Uygulama kapalıyken işletim sistemi birkaç dakikada bir `dbnero --run-due` başlatır (`core/os_scheduler.py`: Windows `schtasks`, macOS LaunchAgent, Linux systemd kullanıcı zamanlayıcısı). Bu mod Qt yüklemez, işi bitince çıkar.

Çakışmaları iki katman önler: süreçler arası dosya kilidi (`scheduler_lock`, katalogun yanında) ve katalogda atomik sahiplenme (`claim_schedule_run`). Görev kullanıcının oturumunda çalıştığı için kayıtlı parolalara (anahtar kasası / DPAPI) erişebilir.
