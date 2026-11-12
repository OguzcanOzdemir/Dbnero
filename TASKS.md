# dbnero — Görev Listesi

Proje dokümanındaki yol haritası (6 aşama), MVP kapsamı ve kabul kriterlerinden türetilmiştir.
İşaretler: `[ ]` yapılacak · `[x]` tamamlandı · **(MVP)** ilk çalışan sürüm için zorunlu.

**Durum:** MVP ve Aşama 1–6 tamamlandı. Açık kalanlar: macOS imzalama/notarization ve Windows/macOS üzerinde arayüzün CI ile doğrulanması.

---

## Aşama 0 — Kararlar ve hazırlık

- [x] Ürün adı `dbnero` olarak sabitlendi; paket adı `dbnero` **(MVP)**
- [x] Lisans seçimi: **MIT** (`LICENSE`)
- [x] Desteklenecek minimum motor sürümleri: PostgreSQL 12+, MySQL 8.0+, SQL Server 2017+
- [x] `docs/backup-compatibility.md`: sürüm kuralları, yöntemler, sınırlamalar
- [x] Paketleme aracı kararı: **PyInstaller** (`packaging/dbnero.spec`; Linux derlemesi doğrulandı)
- [x] Katalog migration yaklaşımı: **basit sürümlü migration** (`PRAGMA user_version`, `storage/migrations.py`)

## Aşama 1 — Temel altyapı

### Proje iskeleti **(MVP)**
- [x] `pyproject.toml` (src-layout, Python 3.12+, bağımlılıklar, `[dev]` extras)
- [x] Dizin yapısı: `src/dbnero/{ui,core,adapters,compare,storage,security}`, `tests/{unit,integration}`, `docs/`
- [x] `python -m dbnero` giriş noktası (`main.py`, `__main__.py`)
- [x] Ruff + mypy + pytest yapılandırması
- [x] `.gitignore`, `.editorconfig`
- [x] GitHub Actions CI: lint + unit test (Windows / macOS / Linux matrisi)
- [x] Logging altyapısı (dosyaya dönen log, parola maskeleme filtresi)
- [x] Uygulama ayarları (arşiv kökü, araç yolları, eşzamanlılık) — platforma uygun config dizini (`platformdirs`)

### Domain modelleri **(MVP)**
- [x] `core/models.py`: `ConnectionProfile`, `BackupRecord`, `BackupBatch`, `Job`, `BackupResult`, `RestoreResult`, `ValidationResult`
- [x] `compare/snapshots.py`: normalize `SchemaSnapshot` modeli (tablo, sütun, tip, null, default, PK, FK, indeks, view, trigger, routine)
- [x] Status enum'ları (pending / running / succeeded / failed / unverified / cancelled)

### Adaptör sözleşmesi **(MVP)**
- [x] `adapters/base.py`: `DatabaseAdapter` Protocol
- [x] Adaptör fabrikası / registry (motor adı → adaptör)
- [x] Ortak `subprocess` yardımcısı: `shell=False`, zaman aşımı, iptal, stderr yakalama, ilerleme
- [x] Harici araç keşfi: `pg_dump`, `pg_restore`, `mysqldump`, `mysql`, ODBC driver; sürüm tespiti

### Güvenlik **(MVP)**
- [x] `security/credentials.py`: keyring sarmalayıcı (kaydet / oku / sil)
- [x] Keyring yoksa (headless Linux) parolalar yalnızca oturum boyunca bellekte tutulur, arayüz uyarır; ilk çalıştırma kontrolü kurulum yönlendirmesi verir
- [x] Araçlara parolayı argümansız iletme: `PGPASSWORD`, MySQL `--defaults-extra-file` (0600, iş sonunda silinir)
- [x] SQL tanımlayıcı doğrulama/kaçışlama yardımcıları (motor bazında)

### SQLite katalog **(MVP)**
- [x] SQLAlchemy modelleri: `connections`, `backups`, `batches`, `jobs`, `job_logs`
- [x] `backups` tablosu dokümandaki tüm alanları içerir (`backup_id` UUID, `batch_id`, `created_at_utc`, `checksum_sha256`, `tool_versions`, `schema_snapshot_path` …)
- [x] `created_at_utc` değiştirilemez; isteğe bağlı `reference_date` ayrı alan
- [x] `storage/catalog.py`: CRUD + filtreleme (tarih, DB, sunucu, motor, etiket)
- [x] Migration altyapısı

### Bağlantı yönetimi **(MVP)**
- [x] Bağlantı profili oluştur / düzenle / sil (host, port, kullanıcı, auth türü, TLS/SSL, varsayılan DB)
- [x] MSSQL: SQL auth + Windows auth (Windows'ta), ODBC driver seçimi, `TrustServerCertificate` seçeneği
- [x] **Bağlantıyı test et**
- [x] Veritabanlarını listeleme; sistem DB'lerini ayırma (`master/model/msdb/tempdb`, `mysql/sys/information_schema/performance_schema`, `template0/template1`)
- [x] Motor sürümü tespiti

### UI temeli **(MVP)**
- [x] `ui/main_window.py`: yan menülü ana pencere (Genel Bakış, Bağlantılar, Yeni Yedek, Arşiv, Geri Yükle, Karşılaştır, İş Geçmişi, Ayarlar)
- [x] `ui/connections_page.py`: profil listesi, form, test butonu, DB keşfi
- [x] İş yürütücü: `core/jobs.py` (Qt'den bağımsız thread pool) + `ui/common.JobBridge` sinyalleri; kısa çağrılar `QThreadPool` ile
- [x] İlk çalıştırma sihirbazı: araç ve sürücü kontrolü, eksikler için kurulum talimatı

**Aşama 1 çıktısı:** Uygulama açılır, bağlantılar güvenli şekilde yönetilir.

## Aşama 2 — Yedekleme

### Arşiv ve manifest **(MVP)**
- [x] `storage/archive.py`: `arşiv/<motor>/<sunucu>/<db>/<yıl>/<ay>/` düzeni, isim sanitize
- [x] Dosya adı: `YYYY-MM-DD_HHMMSS_<motor>_<db>_<kısa_id>.<uzantı>`
- [x] Geçici dosyaya yazma → SHA-256 → atomik rename
- [x] `storage/manifests.py`: `manifest.json` yaz/oku (sır içermez)
- [x] Manifest'lerden kataloğu yeniden içe aktarma (katalog kaybı senaryosu)

### Motor adaptörleri — yedek **(MVP)**
- [x] **PostgreSQL:** `pg_dump --format=custom` → `.dump`
- [x] **MySQL:** `mysqldump` → `.sql` / `.sql.gz`; InnoDB için `--single-transaction`; routine/trigger/event seçenekleri; MyISAM tutarlılık uyarısı
- [x] **MSSQL:** `BACKUP DATABASE ... TO DISK` (sunucu tarafı), `WITH CHECKSUM, COPY_ONLY` seçenekleri
- [x] **MSSQL:** `.bak`ı yerel arşive alma stratejisi (paylaşımlı dizin / UNC yolu; yoksa "sunucuda kaldı" olarak işaretle)
- [x] Yedek sırasında **şema anlık görüntüsü** (`inspect_schema`) alıp JSON olarak yanına kaydetme
- [x] Araç sürümlerini `tool_versions` alanına yazma

### Yedek servisleri **(MVP)**
- [x] `core/backup_service.py`: tekil yedek akışı (seç → açıklama/etiket → yedekle → doğrula → kataloğa ekle)
- [x] Toplu yedek: `batch_id`, **her DB için ayrı dosya + ayrı katalog kaydı**
- [x] Bir alt işin hatası diğerlerini etkilemez; DB bazında başarı/başarısızlık raporu
- [x] Eşzamanlılık sınırı, iptal, sıralı mod
- [x] Temel doğrulama: dosya var / boyut > 0 / checksum / `pg_restore --list` / `RESTORE VERIFYONLY` / SQL dosyası sonu kontrolü

### UI **(MVP)**
- [x] Yeni Yedek ekranı: tekil/toplu seçim, klasör, açıklama, etiket, seçenekler
- [x] `ui/archive_page.py` — Yedek Arşivi: arama, filtre (tarih/DB/sunucu/motor/etiket), boyut, durum, doğrula
- [x] Toplu iş sonuç raporu
- [x] İş Geçmişi ekranı: devam eden/biten işler, hata ve günlükler

**Aşama 2 çıktısı:** Ayrı dosyalarla güvenilir yedek arşivi.

## Aşama 3 — Geri yükleme

- [x] `core/restore_service.py` **(MVP)**
- [x] Ön kontroller: motor eşleşmesi, sürüm uyumu, format, hedef yetkileri, disk alanı **(MVP)**
- [x] Varsayılan hedef: **yeni veritabanı**; üzerine yazma varsayılan kapalı **(MVP)**
- [x] Üzerine yazma için açık risk onayı (DB adını yazarak onaylama gibi) **(MVP)**
- [x] İsteğe bağlı koruyucu ön-yedek **(MVP)**
- [x] **PostgreSQL:** `pg_restore` (hedef DB oluşturma, `--no-owner` seçenekleri) **(MVP)**
- [x] **MySQL:** `mysql` istemcisiyle kontrollü içe aktarma (`.sql.gz` akışla) **(MVP)**
- [x] **MSSQL:** `RESTORE DATABASE ... WITH MOVE` (dosya yolu eşlemesi, `RESTORE FILELISTONLY`) **(MVP)**
- [x] Geri yükleme sonrası doğrulama (bağlan, tablo sayısı, şema snapshot karşılaştırması) **(MVP)**
- [x] `ui/restore_page.py`: uyumluluk kontrolleri, hedef seçimi, risk onayı, ilerleme **(MVP)**
- [x] Sonuç ve hata günlükleri kataloğa **(MVP)**

**Aşama 3 çıktısı:** Güvenli geri yükleme akışı.

## Aşama 4 — Şema karşılaştırma

- [x] Her adaptörde `inspect_schema` → normalize `SchemaSnapshot` **(MVP)**
  - [x] PostgreSQL: `information_schema` + `pg_catalog`
  - [x] MySQL: `information_schema`
  - [x] MSSQL: `sys.*` katalog görünümleri
- [x] `compare/schema_diff.py`: tablo/sütun ekle-sil, tip, null, default, PK/FK, indeks, view/trigger/routine farkları **(MVP)**
- [x] `core/compare_service.py`: **yedek ↔ canlı** (snapshot vs canlı inspect) **(MVP)**
- [x] **Yedek ↔ yedek** (iki snapshot) **(MVP)**
- [x] Snapshot yoksa kullanıcıya sınırlı analiz uyarısı
- [x] `ui/compare_page.py`: kaynak seçimi, kategori/nesne bazlı ağaç görünümü, fark detayı **(MVP)**
- [x] Rapor dışa aktarma: JSON / HTML / CSV

**Aşama 4 çıktısı:** Yedek–canlı / yedek–yedek şema farkları.

## Aşama 5 — İleri karşılaştırma (MVP sonrası)

- [x] İzole/geçici veritabanına otomatik geri yükleme ve temizlik
- [x] Tablo bazında kayıt sayısı karşılaştırması
- [x] Kontrol özeti karşılaştırması (anahtarsız tablolar için sıradan bağımsız parmak izi)
- [x] `compare/data_diff.py`: birincil anahtar bazlı eklenen / silinen / değişen satırlar (benzersiz indeksler henüz kullanılmıyor)
- [x] Anahtarsız tablolar için sınırlı/özet analiz
- [x] Büyük tablolar için parçalı okuma (sunucu tarafı imleç / `fetchmany`), tablo seçimi ve yalnızca-sayım modu (satır örneklemesi yok)
- [x] Hassas veri: örnek satırlar arayüzde gösterilmez, rapora yalnızca açık onayla eklenir

**Aşama 5 çıktısı:** Veri fark analizi.

## Aşama 6 — Dağıtım ve kalite

### Test
- [x] Birim testleri: servisler, katalog, manifest, diff motoru, adaptörler (mock'lu) **(MVP)**
- [x] `docker-compose` ile MSSQL / MySQL / PostgreSQL test ortamı **(MVP)**
- [x] Entegrasyon: yedekle → doğrula → geri yükle → şema karşılaştır döngüsü (her motor) **(MVP)**
- [x] Sürüm matrisi testleri (birden fazla motor sürümü)
- [x] UI testleri (`pytest-qt`)

### Paketleme
- [x] Windows paketi (PyInstaller, `.zip`) — CI'da derlenir
- [ ] macOS paketi: `.app` CI'da derlenir; **imzalama/notarization yapılmadı** (Apple geliştirici sertifikası gerekir)
- [x] Linux paketi (PyInstaller, `.tar.gz`; derleme ve açılış bu ortamda doğrulandı)
- [x] CI'da üç platform için otomatik build (`.github/workflows/release.yml`)
- [x] Kurulum dokümantasyonu: harici araçların her platformda kurulumu

### Diğer
- [x] Genel Bakış ekranı: son yedekler, başarısız işler, kullanılan alan
- [x] Ayarlar ekranı: araç yolları, saklama politikası, disk doluluk uyarısı, eşzamanlılık
- [x] Yedek silme öncesi onay mekanizması
- [x] `docs/architecture.md`

**Aşama 6 çıktısı:** Dağıtıma hazır sürüm.

---

## Kabul kriterleri (MVP)

- [ ] Windows, macOS ve Linux üzerinde arayüz açılabiliyor — Linux'ta doğrulandı; Windows/macOS CI birim testleriyle (pytest-qt, offscreen) doğrulanacak
- [x] MSSQL, MySQL ve PostgreSQL bağlantıları ayrı ayrı test edilebiliyor
- [x] Tek veritabanı yedeği alınabiliyor ve arşivde görünüyor
- [x] Seçilen en az iki veritabanı topluca yedekleniyor ve **iki ayrı yedek dosyası** üretiliyor
- [x] Her yedeğin gerçek tarihi, açıklaması, kimliği ve SHA-256 bilgisi kaydediliyor
- [x] Yedeğin bozulduğu veya eksik olduğu anlaşılabiliyor
- [x] Uyumlu bir hedefe geri yükleme test ortamında başarıyla tamamlanıyor
- [x] Var olan veritabanının üzerine yazmadan önce açık onay alınıyor
- [x] Şema anlık görüntüsü bulunan yedek ile canlı veritabanının şema farkları gösteriliyor
- [x] Şema anlık görüntüsü bulunan iki yedeğin şema farkları gösteriliyor
- [x] Başarısız bir toplu yedekleme alt işi, diğer yedeklerin durumunu gizlemiyor
- [x] Parolalar uygulama kataloğunda veya günlüklerde düz metin bulunmuyor

---

## v0.1.0 — Yayın, arayüz yenilemesi ve kod incelemesi

- [x] Uygulama tamamen İngilizce (arayüz ve servis mesajları)
- [x] Yeni tasarım sistemi: açık/koyu tema token'ları, ikonlu kenar çubuğu, kartlar, durum rozetleri, gizlenebilir gelişmiş ayarlar
- [x] Uygulama ikonu (SVG, .ico, .icns, .png)
- [x] Windows kurulum dosyası (Inno Setup) + taşınabilir .zip, macOS .dmg (Apple Silicon), Linux AppImage + .tar.gz
- [x] `v*` etiketiyle otomatik GitHub Release ve `SHA256SUMS.txt`
- [x] GitHub Pages indirme sitesi (`site/`), işletim sistemine göre önerilen indirme
- [x] İngilizce README ve indirme bağlantıları
- [x] Tam kod incelemesi (10 bulgu) ve düzeltmeleri:
  - [x] Manifest'ten içe aktarmada toplu iş kaydı yeniden oluşturuluyor
  - [x] MySQL / SQL Server / PostgreSQL: varsayılan veritabanının üzerine geri yükleme güvenli
  - [x] SQL Server: silme başarısızsa veritabanı tek kullanıcı modunda kalmıyor
  - [x] Saklama politikası başarısız kayıtları yer tutucu saymıyor, son kullanılabilir yedeği koruyor
  - [x] Yarıda kalan yedek kayıtları açılışta "failed" olarak işaretleniyor
  - [x] PostgreSQL 12 ve öncesinde `DROP DATABASE` destekleniyor
  - [x] Doğrulama iptal edilebilir
  - [x] Türkçe karakterli arama (Unicode büyük/küçük harf)
  - [x] SQL Server SQL aktarımı dosyayı bir kez okuyor
  - [x] Arayüz veritabanı keşfini servis üzerinden yapıyor
- [x] GitHub Pages etkinleştirildi
- [x] PostgreSQL ve MySQL istemci araçları pakete gömülü (kullanıcı kurulum yapmaz); SQL Server için Windows'un gömülü ODBC sürücüsü
- [x] Açılır listelerde ok işareti; Karşılaştır'da veritabanı listeden seçiliyor
- [x] Geri Yükle: sunucu ve hedef seçimi ("yeni veritabanı" / "var olanın yerine" listeden), orijinal ad otomatik
- [x] Yedekler: etiket filtresi listeden
- [x] Windows 1312 (Kimlik Bilgisi Yöneticisi) çökmesi, konsol pencereleri, log klasörü yazılamazsa çökme düzeltildi
- [x] Koyu temada onay kutuları, seçenek düğmeleri ve açılır ok işaretleri görünüyor (QProxyStyle ile çiziliyor)
- [x] Karşılaştır: "Added/Changed" yerine "Only in From / Only in To / Different"; karşılaştırmanın salt-okunur olduğu belirtiliyor
- [x] Veri karşılaştırma: yalnızca bir tarafta olan tablolar da satır sayısıyla listeleniyor (tamamen farklı veritabanları boş gelmiyor)
- [x] Yedek seçimlerinde açıklama ve etiketler görünüyor; Yedekler tablosunda Tags sütunu
- [x] Bağlantılarda "Save password" (varsayılan açık); Windows'ta kasa yoksa DPAPI ile şifreli dosya
- [x] Geri Yükle: tek hedef alanı; ad varsa onay kutusuyla yerine yazılır, yoksa oluşturulur
- [x] Geri Yükle: başka bir iş bitince seçilen sunucu/veritabanı sıfırlanmıyor (0.1.5)
- [x] Zamanlanmış yedekler (0.2.0): her gün / seçili günler / birkaç saatte bir, "son N yedeği tut", uygulama kapalıyken de çalışır (Windows Görev Zamanlayıcı, macOS launchd, Linux systemd), kaçırılan çalıştırma telafi edilir, süreçler arası kilit
- [x] Uygulama içi bildirimler (0.3.0): kaçırılan ve başarısız zamanlanmış yedekler açılışta Ana sayfada + sistem bildirimi
- [x] Kullanım Şartları + Gizlilik Politikası: ilk açılışta bir kez onay, Windows kurulumunda lisans sayfası, sitede sayfalar
- [x] 0.3.1: parola deposu yarış durumu düzeltildi (Windows'ta kayıtlı parolanın "yok" görünmesi), eksik parola için açık mesaj
- [x] 0.3.1: açılır listelerde siyah kenar ve iç içe kutu düzeltildi; tüm tablolar ortak `DataTable` ile (satır vurgusu, sığdırma, tooltip), ayrıntı paneli düzenlendi
- [ ] macOS/Linux için SQL Server ODBC sürücüsünü gömmek (sürücü sistem bileşeni; ileride saf-Python TDS sürücüsü değerlendirilebilir)
- [ ] Kod imzalama (Windows Authenticode, Apple Developer ID + notarization)
- [ ] macOS Intel derlemesi

## Gelecek fikirleri (kapsam dışı)

- Bulut depolama (S3, Azure Blob, GCS)
- AES-GCM şifreli arşiv
- PostgreSQL global nesneleri (`pg_dumpall --globals-only`)
- MySQL fiziksel yedek (XtraBackup)
- Motorlar arası taşıma/dönüştürme
- Ekip bazlı yetkilendirme, merkezi/web yönetim
