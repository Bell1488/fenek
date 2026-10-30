# Развертывание Fenek.pro на VPS

Приложение состоит из статических страниц в `dist/` и Python WSGI-приложения. Gunicorn обслуживает сайт и `/api/lead` на `127.0.0.1:8080`, Nginx принимает внешний HTTP(S)-трафик, а SQLite хранит обращения в `private/`. Уведомление отправляется в Telegram только через SOCKS5 с DNS на стороне прокси (`socks5h`); прямого резервного соединения нет.

## Требования

- Ubuntu 22.04/24.04 или Debian 12, доступ по SSH с правами sudo.
- Домен `fenek.pro` и `www.fenek.pro` указывают A/AAAA-записями на VPS. Убедитесь, что AAAA-запись ведет на рабочий IPv6 адрес сервера, иначе удалите ее.
- Открыты входящие TCP-порты 80 и 443. SSH ограничен вашим административным IP, если это возможно.
- Рабочий SOCKS5 прокси, доступный с VPS, токен Telegram-бота и ID чата.
- Если собираетесь включать аналитику, ID счетчика Яндекс Метрики.

Проверьте DNS у регистратора и дождитесь его обновления до выпуска сертификата:

```sh
dig +short A fenek.pro
dig +short A www.fenek.pro
dig +short AAAA fenek.pro
```

## 1. Установка кода

Подключитесь к VPS и установите пакеты:

```sh
sudo apt update
sudo apt install -y git python3 python3-venv curl nginx certbot python3-certbot-nginx
```

Создайте системную учетную запись и каталог приложения, затем клонируйте репозиторий:

```sh
sudo useradd --system --home /opt/fenek --create-home --shell /usr/sbin/nologin fenek
sudo -u fenek git clone https://github.com/Bell1488/fenek.git /opt/fenek
sudo install -d -o fenek -g fenek -m 0700 /opt/fenek/private
```

Если репозиторий приватный, настройте на VPS SSH-доступ к GitHub и используйте адрес вида `git@github.com:Bell1488/fenek.git`.

## 2. Конфигурация секретов

Создайте `.env` на VPS. Не переносите заполненный `.env` с рабочей станции через Git и не вставляйте его в команды, попадающие в историю shell. Откройте редактор:

```sh
sudo -u fenek nano /opt/fenek/.env
```

Заполните шаблон, используя актуальные значения:

```dotenv
TELEGRAM_BOT_TOKEN=ВСТАВЬТЕ_ТОКЕН_БОТА
TELEGRAM_CHAT_ID=ВСТАВЬТЕ_ID_ЧАТА
SOCKS_PROXY_URL=socks5h://USER:PASSWORD@HOST:PORT
ALLOWED_ORIGINS=https://fenek.pro,https://www.fenek.pro
LEAD_DB_PATH=/opt/fenek/private/leads.sqlite3
TRUST_REVERSE_PROXY=1
TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128
```

Проверьте, что специальные символы в логине/пароле прокси корректно URL-кодированы. Ограничьте доступ к файлу:

```sh
sudo chown fenek:fenek /opt/fenek/.env
sudo chmod 0600 /opt/fenek/.env
```

Файл должен содержать по одной переменной `KEY=value` на строку. Не публикуйте его в тикетах, логах или Git.

## 3. Python и проверка Telegram

Создайте виртуальное окружение и установите зависимости:

```sh
cd /opt/fenek
sudo -u fenek python3 -m venv .venv
sudo -u fenek .venv/bin/python -m pip install --upgrade pip
sudo -u fenek .venv/bin/python -m pip install -r requirements.txt
```

Проверьте доступность бота и чата. Скрипт выполняет только чтение и не отправляет тестовое сообщение:

```sh
cd /opt/fenek
sudo -u fenek .venv/bin/python scripts/check-telegram.py
```

Если проверка не проходит, сначала исправьте прокси, токен, ID чата или права бота. Отправка намеренно не переключается на прямое соединение.

## 4. Запуск приложения через systemd

Установите готовые unit-файлы и включите сервис приложения и почасовую очистку базы:

```sh
sudo cp /opt/fenek/docs/fenek.service /etc/systemd/system/
sudo cp /opt/fenek/docs/fenek-cleanup.service /etc/systemd/system/
sudo cp /opt/fenek/docs/fenek-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fenek.service fenek-cleanup.timer
sudo systemctl status fenek.service --no-pager
curl -I http://127.0.0.1:8080/
```

Ожидается HTTP 200. Gunicorn слушает только loopback-интерфейс; порт 8080 не нужно открывать в firewall. Логи состояния службы доступны командой `sudo journalctl -u fenek.service -n 100 --no-pager`; приложение не пишет содержимое заявок и секреты в журнал.

## 5. HTTPS и Nginx

До выпуска сертификата убедитесь, что DNS уже указывает на VPS и Nginx доступен снаружи по порту 80. Получите сертификат:

```sh
sudo certbot certonly --nginx -d fenek.pro -d www.fenek.pro
```

Установите конфигурацию сайта:

```sh
sudo cp /opt/fenek/docs/nginx-fenek.conf /etc/nginx/sites-available/fenek
sudo ln -s /etc/nginx/sites-available/fenek /etc/nginx/sites-enabled/fenek
sudo nginx -t
sudo systemctl reload nginx
```

Если ссылка уже существует, не создавайте ее повторно. При необходимости отключите стандартный сайт `/etc/nginx/sites-enabled/default`, проверив, что на VPS он не обслуживает другой проект. Конфигурация переводит HTTP и `www.fenek.pro` на `https://fenek.pro`, проксирует сайт на Gunicorn и не ведет access log с URL-параметрами.

## 6. Проверка сайта и формы

Проверьте редирект и страницы:

```sh
curl -I http://fenek.pro/
curl -I https://fenek.pro/
curl -I https://www.fenek.pro/cargo.html
```

На сайте отправьте одну тестовую заявку и подтвердите ее получение в целевом Telegram-чате. Затем проверьте журнал сервиса и состояние базы:

```sh
sudo journalctl -u fenek.service -n 100 --no-pager
cd /opt/fenek
sudo -u fenek .venv/bin/python backend/server.py pending
```

Не отправляйте одну и ту же тестовую заявку многократно: повтор доставленной заявки не создает новое сообщение, а после неопределенного сетевого результата автоматический повтор отключен, чтобы избежать дубля.

## 7. Яндекс Метрика

До включения аналитики создайте счетчик, настройте требуемые цели и проверьте режим согласия и тексты политики. Укажите публичный числовой ID в `dist/config.js` вместо `metrikaId: 0`. Не добавляйте туда секреты. После изменения разверните обновленный файл и проверьте события в отладчике Метрики; форма должна считать основной конверсией подтвержденную доставку, а не клик или попытку отправки.

## Обновление

Сначала сохраните резервную копию базы SQLite. Затем получите изменения из GitHub, обновите зависимости при изменении `requirements.txt` и перезапустите приложение:

```sh
sudo -u fenek git -C /opt/fenek pull --ff-only
sudo -u fenek /opt/fenek/.venv/bin/python -m pip install -r /opt/fenek/requirements.txt
sudo systemctl restart fenek.service
sudo systemctl status fenek.service --no-pager
```

Резервную копию базы храните с ограниченным доступом и отдельно от публичного каталога сайта. Не копируйте каталог `private/` поверх активной базы при обновлении.

## Диагностика

- Состояние служб: `sudo systemctl status fenek.service fenek-cleanup.timer --no-pager`.
- Журнал приложения: `sudo journalctl -u fenek.service -n 100 --no-pager`.
- Ошибки Nginx: `sudo journalctl -u nginx -n 100 --no-pager`.
- Синтаксис Nginx: `sudo nginx -t`.
- Проверка Telegram через SOCKS: `sudo -u fenek /opt/fenek/.venv/bin/python /opt/fenek/scripts/check-telegram.py`.
- Неотправленные обращения: `sudo -u fenek /opt/fenek/.venv/bin/python /opt/fenek/backend/server.py pending`.

Команда `backend/server.py show UUID` выводит персональные данные обращения. Выполняйте ее только из административной консоли с ограниченным доступом.
