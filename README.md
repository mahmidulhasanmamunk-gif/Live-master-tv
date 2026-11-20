# IPTV-MASTER

একাধিক বৈধ M3U Source থেকে Channel সংগ্রহ করে একটি Category-সাজানো, Duplicate-মুক্ত, স্বয়ংক্রিয়ভাবে আপডেট হওয়া Master Playlist তৈরির সিস্টেম। Python + GitHub Actions, কোনো Paid API বা API Key লাগে না।

## ফাইল কাঠামো

```
IPTV-MASTER/
├── master.m3u              # চূড়ান্ত Playlist (প্রতি Channel-এ সেরা Stream)
├── master_failover.m3u     # (ঐচ্ছিক) Failover Proxy-লিংকসহ Playlist
├── channels.json           # সব Channel, সব Alternative URL, যাচাইয়ের ফলাফল, পরিসংখ্যান
├── failed_sources.json     # Source ব্যর্থতা + ব্যর্থ Stream-এর তালিকা
├── sources.json            # Source তালিকা, ফিল্টার, সেটিংস (এখানে নতুন Source যোগ করুন)
├── update.py               # মূল প্রসেসিং
├── failover_proxy.py       # প্রকৃত Automatic Failover সার্ভার
├── requirements.txt
└── .github/workflows/update.yml
```

## Source বিশ্লেষণ: কী গ্রহণ ও কী বাদ

আমি Source 1 (Mrgify-BDIX-IPTV) খুলে দেখেছি। এতে তিন ধরনের এন্ট্রি আছে:

1. **অ্যাকাউন্ট-বাঁধা Token URL**: `aynaott.com`-এর লিংকে `e=`, `u=<ইউজার আইডি>`, `token=` আছে। এগুলো একটি নির্দিষ্ট অ্যাকাউন্টের মেয়াদি লিংক। **বাদ।**
2. **Toffee**: `toffeelive.com` লিংকের সাথে `Toffee (Linux;Android 14)` নকল User-Agent লাগে। **বাদ।**
3. **সরাসরি খোলা URL**: যেমন সরাসরি সম্প্রচারকের সার্ভার, Amagi/Wurl/Samsung-এর FAST চ্যানেল। এগুলোর কোনো Token বা Header লাগে না। **গ্রহণ।**

Source 2, 3 ও 4 আমি খুলে দেখিনি। একই ফিল্টার সেগুলোতে স্বয়ংক্রিয়ভাবে প্রযোজ্য হবে। প্রতি রানে `channels.json`-এর `meta.sources` অংশে প্রতিটি Source থেকে কতটি এন্ট্রি এসেছে, কতটি রাখা হয়েছে এবং কোন কারণে কতটি বাদ গেছে তা লেখা থাকে। প্রথম রানের পর সেটি দেখে নিন।

### ফিল্টার নিয়ম (`sources.json` → `filters`)

| বাদ যায় | কারণ কোড |
|---|---|
| `blocked_domains`-এর ডোমেইন (ডিফল্ট: aynaott.com, toffeelive.com) | `blocked_domain` |
| URL-এ `user:pass@` বা `@` | `credentials_in_url` |
| `/live/<user>/<pass>/<id>.ts` ধরনের পাথ | `account_credentials_in_path` |
| Query-তে token, e, u, sig, expires, auth ইত্যাদি | `token_or_auth_in_query` |
| `#EXTVLCOPT:http-user-agent/referrer/origin`, `#EXTHTTP`, `url\|Header=` | `needs_custom_headers` |
| `#KODIPROP` License/DRM, HLS SAMPLE-AES, DASH ContentProtection | `drm_protected` |
| http/https ছাড়া স্কিম (যেমন `hls://`) | `unsupported_scheme_or_host` |
| Radio/অডিও-অনলি | `radio_or_audio_only` |
| Promo/Placeholder নাম | `promo_or_placeholder` |

**গুরুত্বপূর্ণ সীমাবদ্ধতা:** এগুলো প্রযুক্তিগত ফিল্টার; কোনো সম্প্রচারের লাইসেন্স আছে কিনা তা কোড জানতে পারে না। কোন Source ও Channel ব্যবহার করবেন তার দায়িত্ব আপনার। শুধু যাদের স্ট্রিম বিনামূল্যে ও প্রকাশ্যে সম্প্রচারের জন্য দেওয়া, তাদের Source যোগ করুন।

## Category ক্রম

1. Bangla News 2. Sports 3. Bangladeshi Entertainment 4. Bangladeshi General TV 5. International News 6. International Sports 7. International Entertainment 8. Movies 9. Music 10. Kids 11. Documentary 12. Others

নির্ধারণ পদ্ধতি: Channel-এর নাম, Source Group, tvg-id (যেমন `.bd` প্রত্যয়) ও পরিচিত বাংলাদেশি চ্যানেলের তালিকা বিশ্লেষণ করে। নিশ্চিত না হলে **Others**।

- **Sports** = বাংলাদেশি Sports চ্যানেল (T Sports, GTV ইত্যাদি); বিদেশি Sports যায় **International Sports**-এ।
- **Bangladeshi Entertainment** = Group/নামে Entertainment, Drama, Natok ইত্যাদি আছে এমন বাংলাদেশি চ্যানেল; বাকি বাংলাদেশি সাধারণ চ্যানেল **Bangladeshi General TV**-তে।
- Kids, Music, Movies, Documentary দেশ নির্বিশেষে নিজ নিজ Category-তে।
- নিয়ম পরিবর্তন করতে `update.py`-এর `TOPIC_PATTERNS`, `BD_KEYS`, `BD_NEWS_KEYS`, `BD_SPORTS_KEYS` সম্পাদনা করুন।

## Duplicate শনাক্তকরণ ও Alternative Stream

Channel মেলানো হয় ক্রমানুসারে: (১) একই tvg-id (`@SD` অংশ বাদ দিয়ে) এবং নাম সামঞ্জস্যপূর্ণ, (২) একই URL ও সামঞ্জস্যপূর্ণ নাম, (৩) Normalize করা নাম (`[BD]`, `(720p)`, `HD`, `(1)(2)...` বাদ), (৪) Fuzzy Match (সীমা ০.৯২)।

ভুল Merge ঠেকাতে Fuzzy Match-এ সংখ্যা মিলতে হবে (`Star Sports 1` ≠ `Star Sports 2`) এবং `News`, `Sports`, `Movies`, `Plus`, `Extra`, `World` ইত্যাদি পার্থক্যকারী শব্দ থাকলে Merge হয় না (`Zee Bangla` ≠ `Zee Bangla Cinema`)।

প্রতিটি Channel-এর সব Stream `channels.json`-এ থাকে (Source পরিচয়, মূল Group/Logo/tvg-id, যাচাইয়ের অবস্থা সহ)। Stream-এর ক্রম: সর্বশেষ যাচাইয়ে ব্যর্থ Stream পিছনে, বাকিগুলো Source Priority অনুযায়ী। `master.m3u`-তে প্রতি Channel-এ শীর্ষ Stream-এর মূল URL বসে।

## প্রকৃত Failover

Static M3U-তে Failover সম্ভব নয়, তাই দুটি স্তর আছে:

1. **`master.m3u`**: প্রতি আপডেটে সেরা (যাচাইকৃত) Stream শীর্ষে থাকে। Player-এ নতুন করে Playlist রিফ্রেশ করলে বদলে যায়।
2. **`failover_proxy.py`**: আপনার নিজের মেশিনে (PC, Raspberry Pi, VPS) চালান।

```bash
pip install -r requirements.txt
python failover_proxy.py --channels https://raw.githubusercontent.com/<user>/<repo>/main/channels.json --port 8080
```

Player-এ `http://<সার্ভারের-IP>:8080/master.m3u` দিন। প্রতিটি চ্যানেল খোলার সময় সার্ভার Alternative Stream-গুলো ক্রমে (সর্বোচ্চ ৬টি) যাচাই করে প্রথম কার্যকরটিতে HTTP 302 Redirect করে। ভিডিও ডেটা সার্ভারের ভেতর দিয়ে যায় না। ফলাফল ৬০ সেকেন্ড ক্যাশ হয়।

GitHub-এ `master_failover.m3u` পেতে Repository Settings → Secrets and variables → Actions → **Variables**-এ `PROXY_BASE_URL` (যেমন `http://203.0.113.5:8080`) যোগ করুন।

## Stream Validation

প্রতিটি Stream-এর জন্য: Playlist ডাউনলোড → Master হলে প্রথম Variant → Media Playlist-এর সর্বশেষ Segment-এর প্রথম ৪KB পড়া (MPEG-TS হলে sync byte যাচাই)। শুধু HTTP 200 দেখে "কার্যকর" ধরা হয় না। Timeout ও নেটওয়ার্ক ত্রুটিতে Retry হয়।

- ফলাফল `ok` / `fail` / `unverified` হিসেবে সংরক্ষিত। যাচাই না হলে `unverified`, কখনো `ok` নয়।
- সাময়িক ব্যর্থতায় Stream মুছে ফেলা হয় না। শুধু পিছনে যায়। পরপর ৩ বার ব্যর্থ হলে "নিশ্চিত ব্যর্থ" চিহ্নিত হয়।
- ৯৫%-এর বেশি Stream একসাথে ব্যর্থ হলে সেটি Runner-এর নেটওয়ার্ক সমস্যা ধরে সেই রানের ফলাফল প্রয়োগ করা হয় না।
- ১৩ মিনিটের সময়সীমার মধ্যে যতগুলো সম্ভব যাচাই হয়; সবচেয়ে পুরোনো যাচাই আগে। কার্যকর Stream ৯০ মিনিট পর পুনরায় যাচাই হয়।

**ভৌগোলিক সীমাবদ্ধতা:** GitHub Actions সাধারণত যুক্তরাষ্ট্রের সার্ভারে চলে। বাংলাদেশ-সীমাবদ্ধ বা BDIX স্ট্রিম সেখান থেকে ব্যর্থ দেখাতে পারে, যদিও আপনার কাছে চলে। তাই ডিফল্টে ব্যর্থ Channel `master.m3u` থেকে বাদ দেওয়া হয় না (`drop_failed_channels: false`)। নির্ভুল ফলাফল চাইলে Self-hosted Runner বা আপনার নিজের মেশিনে `python update.py` চালান। আর `failover_proxy.py` তো আপনার নিজের নেটওয়ার্ক থেকে যাচাই করে, তাই সেটি এই সমস্যায় পড়ে না।

## Source বন্ধ থাকলে

কোনো Source ডাউনলোড ব্যর্থ হলে (বা হঠাৎ ৭০%-এর বেশি এন্ট্রি কমে গেলে) সেই Source-এর আগের সফল ডেটা ব্যবহার হয় এবং `failed_sources.json`-এ কারণ ও ধারাবাহিক ব্যর্থতার সংখ্যা লেখা হয়। মোট Channel সংখ্যা আগের অর্ধেকের নিচে নামলে বা শূন্য হলে আপডেট বাতিল হয় ও আগের ফাইল অক্ষত থাকে।

## নতুন Source যোগ করা

`sources.json`-এর `sources` তালিকায় যোগ করুন:

```json
{ "id": "my-source", "name": "My Source", "url": "https://example.com/list.m3u", "priority": 8, "enabled": true }
```

`priority` যত ছোট, Source তত অগ্রাধিকার পায়। `id` অনন্য হতে হবে। লোকাল ফাইলও দেওয়া যায় (`file:///path/list.m3u`)। iptv-org-এর `languages/ben.m3u` ও Free-TV প্রস্তুত অবস্থায় `enabled: false` দেওয়া আছে। চালু করতে `true` করুন।

## GitHub-এ চালু করার ধাপ

1. GitHub-এ নতুন Repository তৈরি করুন (যেমন `IPTV-MASTER`)।
2. এই ফোল্ডারের সব ফাইল (`.github/workflows/update.yml` সহ) একই কাঠামোতে Repository-র রুটে আপলোড করুন।
3. **Settings → Actions → General → Workflow permissions** এ গিয়ে **Read and write permissions** নির্বাচন করে Save করুন।
4. **Actions** ট্যাবে `Update IPTV Master Playlist` খুলে **Run workflow** চাপুন (প্রথমবার `skip_validation` টিক দিয়ে দ্রুত চালাতে পারেন)।
5. রান শেষে `master.m3u` ও `channels.json` আপডেট হয়েছে কিনা দেখুন। Raw লিংক: `https://raw.githubusercontent.com/<user>/<repo>/main/master.m3u`
6. এরপর প্রতি ৩০ মিনিটে নিজে নিজে চলবে। ম্যানুয়ালি চালাতে আবার **Run workflow**।

দ্রষ্টব্য: GitHub-এর Schedule কখনো কয়েক মিনিট দেরিতে চলে। Repository-তে ৬০ দিন কোনো কার্যকলাপ না থাকলে GitHub Scheduled Workflow বন্ধ করে দেয়। এই সিস্টেম নিজেই নিয়মিত কমিট করে, তাই সাধারণত সমস্যা হয় না।

## লোকালি চালানো

```bash
pip install -r requirements.txt
python update.py                 # পূর্ণ আপডেট
python update.py --no-validate   # যাচাই ছাড়া
```

## মূল সেটিংস (`sources.json` → `settings`)

| সেটিং | ডিফল্ট | অর্থ |
|---|---|---|
| `validate` | true | Stream যাচাই চালু/বন্ধ |
| `validation_time_budget_seconds` | 780 | যাচাইয়ের সর্বোচ্চ সময় |
| `recheck_ok_minutes` | 90 | কার্যকর Stream পুনরায় যাচাইয়ের বিরতি |
| `failure_threshold` | 3 | পরপর কত বার ব্যর্থ হলে "নিশ্চিত ব্যর্থ" |
| `drop_failed_channels` | false | সব Stream নিশ্চিত ব্যর্থ হলে Channel বাদ দেবে কিনা |
| `embed_alternatives_in_master` | false | `master.m3u`-তে `#ALT-SOURCE:` মন্তব্য লাইন যোগ |
| `fuzzy_threshold` | 0.92 | Fuzzy Match-এর সীমা |
