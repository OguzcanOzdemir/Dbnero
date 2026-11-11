# dbnero — Görev Listesi

Proje dokümanındaki yol haritası (6 aşama), MVP kapsamı ve kabul kriterlerinden türetilmiştir.
İşaretler: `[ ]` yapılacak · `[x]` tamamlandı · **(MVP)** ilk çalışan sürüm için zorunlu.

---

## Aşama 0 — Kararlar ve hazırlık

- [ ] Ürün adı `dbnero` olarak sabitlendi; paket adı `dbnero` **(MVP)**
- [ ] Lisans seçimi (MIT / Apache-2.0 / özel)
- [ ] Desteklenecek minimum motor sürümlerini belirle (ör. PostgreSQL 13+, MySQL 8.0+, SQL Server 2017+)
- [ ] `docs/backup-compatibility.md`: motor sürümü × araç sürümü uyumluluk matrisi taslağı
- [ ] Paketleme aracı kararı: PyInstaller mı Nuitka mı (kısa PoC ile)
- [ ] Katalog migration yaklaşımı: Alembic mi basit sürümlü migration mı

## Aşama 1 — Temel altyapı

### Proje iskeleti **(MVP)**
- [ ] `pyproject.toml` (src-layout, Python 3.12+, bağımlılıklar, `[dev]` extras)
- [ ] Dizin yapısı: `src/dbnero/{ui,core,adapters,compare,storage,security}`, `tests/{unit,integration}`, `docs/`
- [ ] `python -m dbnero` giriş noktası (`main.py`, `__main__.py`)
- [ ] Ruff + mypy + pytest yapılandırması
- [ ] `.gitignore`, `.editorconfig`
- [ ] GitHub Actions CI: lint + unit test (Windows / macOS / Linux matrisi)
- [ ] Logging altyapısı (dosyaya dönen log, parola maskeleme filtresi)
- [ ] Uygulama ayarları (arşiv kökü, araç yolları, eşzamanlılık) — platforma uygun config dizini (`platformdirs`)

### Domain modelleri **(MVP)**
- [ ] `core/models.py`: `ConnectionProfile`, `BackupRecord`, `BackupBatch`, `Job`, `BackupResult`, `RestoreResult`, `ValidationResult`
- [ ] `compare/snapshots.py`: normalize `SchemaSnapshot` modeli (tablo, sütun, tip, null, default, PK, FK, indeks, view, trigger, routine)
- [ ] Status enum'ları (pending / running / succeeded / failed / unverified / cancelled)

### Adaptör sözleşmesi **(MVP)**
- [ ] `adapters/base.py`: `DatabaseAdapter` Protocol
- [ ] Adaptör fabrikası / registry (motor adı → adaptör)
- [ ] Ortak `subprocess` yardımcısı: `shell=False`, zaman aşımı, iptal, stderr yakalama, ilerleme
- [ ] Harici araç keşfi: `pg_dump`, `pg_restore`, `mysqldump`, `mysql`, ODBC driver; sürüm tespiti

### Güvenlik **(MVP)**
- [ ] `security/credentials.py`: keyring sarmalayıcı (kaydet / oku / sil)
- [ ] Keyring yoksa (headless Linux) anlaşılır hata ve yönlendirme
- [ ] Araçlara parolayı argümansız iletme: `PGPASSWORD` / geçici `.pgpass`, MySQL `--defaults-extra-file` (0600, iş sonunda silinir)
- [ ] SQL tanımlayıcı doğrulama/kaçışlama yardımcıları (motor bazında)

### SQLite katalog **(MVP)**
- [ ] SQLAlchemy modelleri: `connections`, `backups`, `batches`, `jobs`, `job_logs`
- [ ] `backups` tablosu dokümandaki tüm alanları içerir (`backup_id` UUID, `batch_id`, `created_at_utc`, `checksum_sha256`, `tool_versions`, `schema_snapshot_path` …)
- [ ] `created_at_utc` değiştirilemez; isteğe bağlı `reference_date` ayrı alan
- [ ] `storage/catalog.py`: CRUD + filtreleme (tarih, DB, sunucu, motor, etiket)
- [ ] Migration altyapısı

### Bağlantı yönetimi **(MVP)**
- [ ] Bağlantı profili oluştur / düzenle / sil (host, port, kullanıcı, auth türü, TLS/SSL, varsayılan DB)
- [ ] MSSQL: SQL auth + Windows auth (Windows'ta), ODBC driver seçimi, `TrustServerCertificate` seçeneği
- [ ] **Bağlantıyı test et**
- [ ] Veritabanlarını listeleme; sistem DB'lerini ayırma (`master/model/msdb/tempdb`, `mysql/sys/information_schema/performance_schema`, `template0/template1`)
- [ ] Motor sürümü tespiti

### UI temeli **(MVP)**
- [ ] `ui/main_window.py`: yan menülü ana pencere (Genel Bakış, Bağlantılar, Yeni Yedek, Arşiv, Geri Yükle, Karşılaştır, İş Geçmişi, Ayarlar)
- [ ] `ui/connections_page.py`: profil listesi, form, test butonu, DB keşfi
- [ ] İş yürütücü: `QThreadPool` + sinyallerle ilerleme / log / iptal
- [ ] İlk çalıştırma sihirbazı: araç ve sürücü kontrolü, eksikler için kurulum talimatı

**Aşama 1 çıktısı:** Uygulama açılır, bağlantılar güvenli şekilde yönetilir.

## Aşama 2 — Yedekleme

### Arşiv ve manifest **(MVP)**
- [ ] `storage/archive.py`: `arşiv/<motor>/<sunucu>/<db>/<yıl>/<ay>/` düzeni, isim sanitize
- [ ] Dosya adı: `YYYY-MM-DD_HHMMSS_<motor>_<db>_<kısa_id>.<uzantı>`
- [ ] Geçici dosyaya yazma → SHA-256 → atomik rename
- [ ] `storage/manifests.py`: `manifest.json` yaz/oku (sır içermez)
- [ ] Manifest'lerden kataloğu yeniden içe aktarma (katalog kaybı senaryosu)

### Motor adaptörleri — yedek **(MVP)**
- [ ] **PostgreSQL:** `pg_dump --format=custom` → `.dump`
- [ ] **MySQL:** `mysqldump` → `.sql` / `.sql.gz`; InnoDB için `--single-transaction`; routine/trigger/event seçenekleri; MyISAM tutarlılık uyarısı
- [ ] **MSSQL:** `BACKUP DATABASE ... TO DISK` (sunucu tarafı), `WITH CHECKSUM, COPY_ONLY` seçenekleri
- [ ] **MSSQL:** `.bak`ı yerel arşive alma stratejisi (paylaşımlı dizin / UNC yolu; yoksa "sunucuda kaldı" olarak işaretle)
- [ ] Yedek sırasında **şema anlık görüntüsü** (`inspect_schema`) alıp JSON olarak yanına kaydetme
- [ ] Araç sürümlerini `tool_versions` alanına yazma

### Yedek servisleri **(MVP)**
- [ ] `core/backup_service.py`: tekil yedek akışı (seç → açıklama/etiket → yedekle → doğrula → kataloğa ekle)
- [ ] Toplu yedek: `batch_id`, **her DB için ayrı dosya + ayrı katalog kaydı**
- [ ] Bir alt işin hatası diğerlerini etkilemez; DB bazında başarı/başarısızlık raporu
- [ ] Eşzamanlılık sınırı, iptal, sıralı mod
- [ ] Temel doğrulama: dosya var / boyut > 0 / checksum / `pg_restore --list` / `RESTORE VERIFYONLY` / SQL dosyası sonu kontrolü

### UI **(MVP)**
- [ ] Yeni Yedek ekranı: tekil/toplu seçim, klasör, açıklama, etiket, seçenekler
- [ ] `ui/backups_page.py` — Yedek Arşivi: arama, filtre (tarih/DB/sunucu/motor/etiket), boyut, durum, doğrula
- [ ] Toplu iş sonuç raporu
- [ ] İş Geçmişi ekranı: devam eden/biten işler, hata ve günlükler

**Aşama 2 çıktısı:** Ayrı dosyalarla güvenilir yedek arşivi.

## Aşama 3 — Geri yükleme

- [ ] `core/restore_service.py` **(MVP)**
- [ ] Ön kontroller: motor eşleşmesi, sürüm uyumu, format, hedef yetkileri, disk alanı **(MVP)**
- [ ] Varsayılan hedef: **yeni veritabanı**; üzerine yazma varsayılan kapalı **(MVP)**
- [ ] Üzerine yazma için açık risk onayı (DB adını yazarak onaylama gibi) **(MVP)**
- [ ] İsteğe bağlı koruyucu ön-yedek **(MVP)**
- [ ] **PostgreSQL:** `pg_restore` (hedef DB oluşturma, `--no-owner` seçenekleri) **(MVP)**
- [ ] **MySQL:** `mysql` istemcisiyle kontrollü içe aktarma (`.sql.gz` akışla) **(MVP)**
- [ ] **MSSQL:** `RESTORE DATABASE ... WITH MOVE` (dosya yolu eşlemesi, `RESTORE FILELISTONLY`) **(MVP)**
- [ ] Geri yükleme sonrası doğrulama (bağlan, tablo sayısı, şema snapshot karşılaştırması) **(MVP)**
- [ ] `ui/restore_dialog.py`: uyumluluk kontrolleri, hedef seçimi, risk onayı, ilerleme **(MVP)**
- [ ] Sonuç ve hata günlükleri kataloğa **(MVP)**

**Aşama 3 çıktısı:** Güvenli geri yükleme akışı.

## Aşama 4 — Şema karşılaştırma

- [ ] Her adaptörde `inspect_schema` → normalize `SchemaSnapshot` **(MVP)**
  - [ ] PostgreSQL: `information_schema` + `pg_catalog`
  - [ ] MySQL: `information_schema`
  - [ ] MSSQL: `sys.*` katalog görünümleri
- [ ] `compare/schema_diff.py`: tablo/sütun ekle-sil, tip, null, default, PK/FK, indeks, view/trigger/routine farkları **(MVP)**
- [ ] `core/compare_service.py`: **yedek ↔ canlı** (snapshot vs canlı inspect) **(MVP)**
- [ ] **Yedek ↔ yedek** (iki snapshot) **(MVP)**
- [ ] Snapshot yoksa kullanıcıya sınırlı analiz uyarısı
- [ ] `ui/compare_page.py`: kaynak seçimi, kategori/nesne bazlı ağaç görünümü, fark detayı **(MVP)**
- [ ] Rapor dışa aktarma: JSON / HTML / CSV

**Aşama 4 çıktısı:** Yedek–canlı / yedek–yedek şema farkları.

## Aşama 5 — İleri karşılaştırma (MVP sonrası)

- [ ] İzole/geçici veritabanına otomatik geri yükleme ve temizlik
- [ ] Tablo bazında kayıt sayısı karşılaştırması
- [ ] Kontrol özeti (checksum) karşılaştırması
- [ ] `compare/data_diff.py`: PK/benzersiz anahtar bazlı eklenen / silinen / değişen satırlar
- [ ] Anahtarsız tablolar için sınırlı/özet analiz
- [ ] Büyük tablolar için parçalı okuma, örnekleme, kullanıcı tanımlı kapsam
- [ ] Hassas veri maskeleme ve dışa aktarma uyarısı

**Aşama 5 çıktısı:** Veri fark analizi.

## Aşama 6 — Dağıtım ve kalite

### Test
- [ ] Birim testleri: servisler, katalog, manifest, diff motoru, adaptörler (mock'lu) **(MVP)**
- [ ] `docker-compose` ile MSSQL / MySQL / PostgreSQL test ortamı **(MVP)**
- [ ] Entegrasyon: yedekle → doğrula → geri yükle → şema karşılaştır döngüsü (her motor) **(MVP)**
- [ ] Sürüm matrisi testleri (birden fazla motor sürümü)
- [ ] UI testleri (`pytest-qt`)

### Paketleme
- [ ] Windows paketi (installer / exe)
- [ ] macOS paketi (`.app`, imzalama/notarization)
- [ ] Linux paketi (AppImage veya benzeri)
- [ ] CI'da üç platform için otomatik build
- [ ] Kurulum dokümantasyonu: harici araçların her platformda kurulumu

### Diğer
- [ ] Genel Bakış ekranı: son yedekler, başarısız işler, kullanılan alan
- [ ] Ayarlar ekranı: araç yolları, saklama politikası, disk doluluk uyarısı, eşzamanlılık
- [ ] Yedek silme öncesi onay mekanizması
- [ ] `docs/architecture.md`

**Aşama 6 çıktısı:** Dağıtıma hazır sürüm.

---

## Kabul kriterleri (MVP)

- [ ] Windows, macOS ve Linux üzerinde arayüz açılabiliyor
- [ ] MSSQL, MySQL ve PostgreSQL bağlantıları ayrı ayrı test edilebiliyor
- [ ] Tek veritabanı yedeği alınabiliyor ve arşivde görünüyor
- [ ] Seçilen en az iki veritabanı topluca yedekleniyor ve **iki ayrı yedek dosyası** üretiliyor
- [ ] Her yedeğin gerçek tarihi, açıklaması, kimliği ve SHA-256 bilgisi kaydediliyor
- [ ] Yedeğin bozulduğu veya eksik olduğu anlaşılabiliyor
- [ ] Uyumlu bir hedefe geri yükleme test ortamında başarıyla tamamlanıyor
- [ ] Var olan veritabanının üzerine yazmadan önce açık onay alınıyor
- [ ] Şema anlık görüntüsü bulunan yedek ile canlı veritabanının şema farkları gösteriliyor
- [ ] Şema anlık görüntüsü bulunan iki yedeğin şema farkları gösteriliyor
- [ ] Başarısız bir toplu yedekleme alt işi, diğer yedeklerin durumunu gizlemiyor
- [ ] Parolalar uygulama kataloğunda veya günlüklerde düz metin bulunmuyor

---

## Gelecek fikirleri (kapsam dışı)

- Zamanlanmış yedekleme ve saklama kuralları
- Bulut depolama (S3, Azure Blob, GCS)
- AES-GCM şifreli arşiv
- PostgreSQL global nesneleri (`pg_dumpall --globals-only`)
- MySQL fiziksel yedek (XtraBackup)
- Motorlar arası taşıma/dönüştürme
- Ekip bazlı yetkilendirme, merkezi/web yönetim
