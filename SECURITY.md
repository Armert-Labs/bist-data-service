# Güvenlik Politikası

## Desteklenen Sürümler

| Sürüm | Destek |
|-------|--------|
| 0.1.x | ✅ |

## Güvenlik Açığı Bildirimi

Bir güvenlik açığı bulursanız **lütfen herkese açık issue açmayın**. Bunun yerine:

- E-posta: **security@armert.com.tr**
- Veya GitHub'da özel güvenlik danışması açın: *Security → Report a vulnerability*

Bildiriminize şunları ekleyin:
- Açığın açıklaması ve etkisi
- Yeniden üretme adımları (PoC)
- Etkilenen sürüm/bileşen

72 saat içinde ilk yanıtı vermeyi hedefliyoruz.

## Kapsam ve Notlar

- Bu servis **~15 dakika gecikmeli, halka açık** piyasa verisi dağıtır; gerçek
  zamanlı/ticari dağıtım BIST lisansı gerektirir.
- API anahtarları, `.env` ve `webhooks.json` **asla** depoya commit edilmemelidir
  (`.gitignore` ile korunur).
- Üretimde: `API_KEYS` (veya `API_KEYS_SHA256`) tanımlayın, `AUTH_REQUIRED=true`,
  `METRICS_PUBLIC=false`, `CORS_ORIGINS`'i daraltın ve servisi ters proxy arkasında
  TLS ile yayınlayın.

## Uygulanan Güvenlik Önlemleri

- Sabit zamanlı API anahtarı doğrulaması (timing-attack kalkanı), çoklu anahtar + SHA-256 hash saklama
- Girdi doğrulama (sembol/period/interval whitelist), rate limiting
- Kaynaklar arası fiyat doğrulama + sanity-check (veri bütünlüğü)
- Root olmayan Docker konteyneri, request-id sanitizasyonu
