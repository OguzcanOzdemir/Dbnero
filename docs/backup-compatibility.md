# Yedek uyumluluğu

Bu belge dbnero'nun her motor için kullandığı yöntemleri, desteklenen sürümleri ve bilinen sınırlamaları özetler.

## Desteklenen sürümler

| Motor | En düşük sunucu sürümü | Test edilen | Gerekli istemci |
|---|---|---|---|
| PostgreSQL | 12 | 14, 16 | `pg_dump` / `pg_restore`, **sunucu ana sürümüne eşit veya daha yeni** |
| MySQL | 8.0 | 8.0, 8.4 | `mysqldump` / `mysql` 8.0+ (MariaDB istemcisi de çalışır) |
| SQL Server | 2017 | 2022 (Linux) | Microsoft ODBC Driver 17 veya 18 |

"Test edilen" sürümler CI'daki entegrasyon matrisinde (`.github/workflows/ci.yml`) yedekle → doğrula → geri yükle → şema/veri karşılaştır döngüsüyle sınanır.

## Yöntemler

| | PostgreSQL | MySQL | SQL Server |
|---|---|---|---|
| Yedek | `pg_dump --format=custom` | `mysqldump --single-transaction --routines --triggers --events --hex-blob` | `BACKUP DATABASE … WITH COPY_ONLY, CHECKSUM` |
| Dosya | `.dump` | `.sql.gz` (varsayılan) veya `.sql` | `.bak` |
| Geri yükleme | `pg_restore --no-owner --no-privileges --exit-on-error` | `mysql --binary-mode` | `RESTORE DATABASE … WITH MOVE, CHECKSUM` |
| Doğrulama | `PGDMP` başlığı, TOC okunabilir, tüm veri blokları okunabilir | gzip bütünlüğü, `-- Dump completed` satırı | MTF başlığı + sunucuda `RESTORE VERIFYONLY` |
| Şema görüntüsü | `pg_catalog` | `information_schema` | `sys.*` |

Her yedek ayrıca SHA-256 ile özetlenir. Checksum dosyanın değişmediğini gösterir; **tek başına geri yüklenebilirlik garantisi değildir**. Bunun için periyodik test geri yüklemeleri önerilir (Geri Yükle ekranı yeni bir veritabanına yükler ve şemayı doğrular).

## Sürüm kuralları

- **PostgreSQL:** `pg_dump` sunucudan eski ise yedek reddedilir. Daha yeni bir sunucudan alınmış yedeği eski sunucuya yüklemek uyarı verir (çoğu zaman çalışır, garanti değildir).
- **MySQL:** Daha yeni sunucudan alınan döküm eski sunucuya yüklenirken uyarı verilir. `utf8mb4_0900_ai_ci` gibi 8.0'a özgü harmanlamalar MariaDB'ye taşınamaz.
- **SQL Server:** Daha yeni sürümden alınmış `.bak` eski sürüme **geri yüklenemez**; ön kontrol bunu hata olarak engeller.

## Yedeklenmeyenler / bilinen sınırlamalar

- **PostgreSQL:** Roller, tablespace'ler ve diğer küme düzeyindeki global nesneler veritabanı yedeğine dahil değildir (`pg_dumpall --globals-only` ayrı bir strateji gerektirir). Geri yüklemede sahiplik ve yetkiler varsayılan olarak atlanır (`--no-owner --no-privileges`).
- **MySQL:** `--single-transaction` yalnızca InnoDB tablolarında tutarlılık sağlar. InnoDB olmayan tablolar varsa yedek günlüğüne uyarı yazılır. Kullanıcılar ve yetkiler (`mysql` sistem veritabanı) yedeğe dahil değildir. Fiziksel yedek (XtraBackup) desteklenmez.
- **SQL Server:**
  - `.bak` dosyası **sunucunun** diskine yazılır. Arşive almanın iki yolu vardır:
    - **Paylaşılan klasör (önerilir):** Bağlantıda `Sunucu yedek klasörü` (sunucunun gördüğü yol) ve `Yerel erişim yolu` (aynı klasörün bu bilgisayardaki yolu, ör. `\\sunucu\Backups`) tanımlanır. Geri yükleme de bu yolla yapılır.
    - **SQL üzerinden:** Paylaşım yoksa dosya `OPENROWSET(BULK …)` ile okunur (`ADMINISTER BULK OPERATIONS` yetkisi gerekir). Sunucudaki kopya yerinde kalır; geri yüklemede bu kopya kullanılır ve SHA-256'sı arşivdekiyle karşılaştırılır (SQL Server 2016+).
  - SQL Server 2019'dan eski sürümlerde varsayılan yedek klasörü sunucudan okunamayabilir; bağlantıda `Sunucu yedek klasörü` alanını doldurun.
  - SQL Server Express yedek sıkıştırmayı desteklemez.
  - Girişler (logins), SQL Agent işleri ve sunucu ayarları yedeğe dahil değildir.
  - Sistem veritabanları (`master`, `model`, `msdb`, `tempdb`) listede gizlenir ve geri yükleme hedefi olamaz.
- **Toplu yedek:** Farklı veritabanlarının aynı anda alınan yedekleri arasında ortak bir tutarlılık anı garanti edilmez.
- **Motorlar arası** (ör. MSSQL → MySQL) geri yükleme desteklenmez.

## Veri karşılaştırma

- Canlı veritabanları doğrudan karşılaştırılır.
- Bir yedek, seçilen sunucuda `dbnero_tmp_<kimlik>` adlı geçici bir veritabanına geri yüklenir, karşılaştırılır ve ardından silinir. Bunun için yeterli disk alanı ve `CREATE DATABASE` yetkisi gerekir.
- Birincil anahtarı olan tablolar satır bazında (eklenen/silinen/değişen), anahtarsız tablolar sıradan bağımsız bir parmak iziyle (aynı/farklı) karşılaştırılır.
- Harmanlamaya (collation) bağlı anahtar sıralaması tespit edilirse tablo bellekte karşılaştırılır (varsayılan sınır 2 milyon satır); daha büyükse yalnızca satır sayıları karşılaştırılır.
- Rapor dışa aktarımında örnek satır verileri **yalnızca açık onayla** dahil edilir.
