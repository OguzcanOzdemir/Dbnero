# dbnero

**Çoklu veritabanı yedekleme, geri yükleme ve karşılaştırma için masaüstü uygulaması.**

dbnero, farklı veritabanı sunucularına bağlanıp tekil veya toplu yedek almayı, her yedeği ayrı bir dosyada saklamayı, yedekleri açıklama/etiket/tarih gibi meta verilerle arşivlemeyi, uygun yedekleri güvenli şekilde geri yüklemeyi ve veritabanları arasındaki **şema/veri farklarını** görüntülemeyi amaçlayan, çapraz platform bir masaüstü uygulamasıdır.

> **Durum:** Planlama / erken geliştirme. Henüz çalışan bir sürüm yoktur. Görev listesi için [TASKS.md](TASKS.md) dosyasına bakın.

| | |
|---|---|
| **Platformlar** | Windows, macOS, Linux |
| **Dil** | Python 3.12+ |
| **Arayüz** | PySide6 (Qt for Python) |
| **Veritabanları** | Microsoft SQL Server, MySQL, PostgreSQL |

---

## Özellikler

### Bağlantı yönetimi
- MSSQL, MySQL ve PostgreSQL için bağlantı profilleri (oluştur / düzenle / sil)
- Host, port, kullanıcı, kimlik doğrulama türü, TLS/SSL seçenekleri
- **Bağlantıyı test et** ve sunucudaki veritabanlarını listeleme (sistem DB'leri ayrılır)
- Parolalar düz metin yerine **işletim sistemi anahtar kasasında** (`keyring`) saklanır
- Motor sürümü ve gerekli yedekleme araçlarının (`pg_dump`, `mysqldump` vb.) kontrolü

### Yedekleme
- **Tekil yedek:** bağlantı + veritabanı seç → açıklama/etiket gir → yedekle → doğrula → kataloğa ekle
- **Toplu yedek:** bir sunucudan birden fazla veritabanı seç → **her veritabanı için ayrı dosya ve ayrı katalog kaydı**
- Toplu işlerde veritabanı bazında başarı/başarısızlık raporu; bir hata diğer yedekleri geçersiz kılmaz
- Eşzamanlılık sınırı, iptal desteği, sıralı işleme seçeneği
- SHA-256 bütünlük özeti, taşınabilir `manifest.json`

Örnek dosya adı: `2026-11-11_204000_postgres_crm_<id>.dump`

### Geri yükleme
- Yedek seç → hedef seç → ön kontrol (motor, sürüm, format, disk, yetki) → **açık onay** → geri yükle → doğrula
- Var olan veritabanının üzerine yazma **varsayılan olarak kapalıdır**
- Yeni/geçici veritabanına geri yükleme önerilir; isteğe bağlı koruyucu ön-yedek
- Geri yükleme yalnızca **aynı motor içinde** desteklenir (ör. PostgreSQL → PostgreSQL)

### Karşılaştırma
- **Yedek ↔ canlı veritabanı** ve **yedek ↔ yedek**
- Şema farkları: tablolar, sütunlar, tipler, null/varsayılan, PK/FK, indeksler, view/trigger/procedure
- Veri farkları (MVP sonrası): kayıt sayıları, anahtar bazlı eklenen/silinen/değişen satırlar
- HTML / JSON / CSV rapor dışa aktarma (hassas veri uyarısıyla)

---

## Motorlara göre yöntemler

| Motor | Yedek | Geri yükleme | Not |
|---|---|---|---|
| PostgreSQL | `pg_dump --format=custom` → `.dump` | `pg_restore` | Roller ve global nesneler DB yedeğine dahil değildir |
| MySQL | `mysqldump` → `.sql` / `.sql.gz` | `mysql` istemcisi | InnoDB için `--single-transaction` |
| MSSQL | `BACKUP DATABASE` → `.bak` | `RESTORE DATABASE` (+ `MOVE`) | `.bak` **sunucunun** dosya sistemine yazılır |

---

## Mimari

```
┌─────────────────────────────────────┐
│          PySide6 Desktop UI         │
│ Bağlantılar / Yedekler / Diff / Jobs│
└──────────────────┬──────────────────┘
                   ▼
┌─────────────────────────────────────┐
│        Application Services         │
│  BackupService / RestoreService     │
│  CompareService / CatalogService    │
└─────────┬─────────────────┬─────────┘
          ▼                 ▼
┌──────────────────┐ ┌──────────────────┐
│ Engine Adapters  │ │  Storage Layer   │
│ - MSSQL          │ │ - SQLite catalog │
│ - MySQL          │ │ - Local archive  │
│ - PostgreSQL     │ │ - Manifest files │
└──────────────────┘ └──────────────────┘
```

Motor bağımlı işler `DatabaseAdapter` protokolünü uygulayan adaptörlerde; karşılaştırma ise normalize edilmiş bir iç şema modeli üzerinde çalışan `SchemaDiffEngine` / `DataDiffEngine` ile yapılır.

### Arşiv düzeni

```
arşiv/<motor>/<sunucu>/<veritabanı>/<yıl>/<ay>/
    2026-11-11_204000_postgres_crm_<id>.dump
    2026-11-11_204000_postgres_crm_<id>.manifest.json
```

---

## Teknoloji yığını

| Katman | Teknoloji |
|---|---|
| Masaüstü arayüz | PySide6 |
| Yerel katalog | SQLite + SQLAlchemy |
| Kimlik bilgileri | `keyring` |
| MSSQL | `pyodbc` (+ Microsoft ODBC Driver) |
| MySQL | `mysql-connector-python` |
| PostgreSQL | `psycopg` |
| İş yürütme | `QThreadPool` / iş kuyruğu |
| Paketleme | PyInstaller veya Nuitka |
| Test | `pytest` + geçici test veritabanları (Docker) |

---

## Gereksinimler

- Python **3.12+**
- Kullanılacak motorlara göre harici araçlar (uygulama ile birlikte gelmez):
  - PostgreSQL: `pg_dump`, `pg_restore` (sunucu sürümüyle uyumlu)
  - MySQL: `mysqldump`, `mysql`
  - MSSQL: [Microsoft ODBC Driver for SQL Server](https://learn.microsoft.com/sql/connect/odbc/microsoft-odbc-driver-for-sql-server) (17 veya 18)

İlk çalıştırma sihirbazı bu araçları kontrol edecek ve eksikler için kurulum talimatı gösterecektir.

---

## Geliştirme ortamı (planlanan)

```bash
git clone https://github.com/OguzcanOzdemir/Dbnero.git
cd Dbnero

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"

# Uygulamayı çalıştır
python -m dbnero

# Testler
pytest tests/unit
pytest tests/integration           # Docker ile test veritabanları gerekir
```

---

## Proje dizini (planlanan)

```
dbnero/
├── README.md
├── CLAUDE.md
├── TASKS.md
├── pyproject.toml
├── src/dbnero/
│   ├── main.py
│   ├── ui/          # main_window, connections_page, backups_page, restore_dialog, compare_page
│   ├── core/        # backup_service, restore_service, compare_service, jobs, models
│   ├── adapters/    # base, mssql, mysql, postgresql
│   ├── compare/     # schema_diff, data_diff, snapshots
│   ├── storage/     # catalog, manifests, archive
│   └── security/    # credentials
├── tests/
│   ├── unit/
│   └── integration/
└── docs/
    ├── architecture.md
    └── backup-compatibility.md
```

---

## Yol haritası

| Aşama | İçerik | Çıktı |
|---|---|---|
| 1 — Temel altyapı | Proje iskeleti, adaptör sözleşmesi, SQLite katalog, güvenli parola saklama | Uygulama açılır, bağlantılar yönetilir |
| 2 — Yedekleme | Üç motor için tekil/toplu yedek, dosya ve manifest yönetimi | Ayrı dosyalarla güvenilir yedek arşivi |
| 3 — Geri yükleme | Uyumluluk kontrolleri, kontrollü restore, doğrulama, günlükler | Güvenli geri yükleme akışı |
| 4 — Şema karşılaştırma | Normalize şema modeli ve diff ekranı | Yedek–canlı / yedek–yedek şema farkları |
| 5 — İleri karşılaştırma | İzole restore, anahtar bazlı veri diff'i, raporlar | Veri fark analizi |
| 6 — Dağıtım ve kalite | Üç platform paketi, kurulum kontrolleri, otomatik testler | Dağıtıma hazır sürüm |

Ayrıntılı görev listesi: [TASKS.md](TASKS.md)

---

## Kapsam dışı (ilk sürüm)

- Motorlar arası veri taşıma / dönüştürme (ör. MSSQL → MySQL)
- Büyük ölçekli, kesin satır bazında veri karşılaştırması
- Otomatik zamanlanmış yedekleme, saklama kuralları ve bulut depolama
- Şifreli arşiv (AES-GCM), merkezi yönetim ve ekip bazlı yetkilendirme
- Farklı veritabanlarının aynı anda alınan yedekleri arasında küresel tutarlılık anı

---

## Güvenlik ilkeleri

- Parolalar yalnızca OS anahtar kasasında tutulur; **log, komut satırı argümanı ve manifest dosyalarına yazılmaz**
- Mümkün olduğunda TLS ve sunucu sertifikası doğrulaması
- Dinamik SQL'de nesne isimleri motora özgü şekilde doğrulanır ve kaçışlanır; değerler için parametre bağlama
- SHA-256 checksum bütünlüğü gösterir, **tek başına başarılı geri yükleme garantisi değildir**
- Yarım kalan yedekler tamamlanmış görünmez (geçici dosya + atomik sonlandırma)
