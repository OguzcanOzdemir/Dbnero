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
