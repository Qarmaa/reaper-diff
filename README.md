# Reaper Diff

Различия между двумя версиями сессии Reaper (.rpp): дорожки, item'ы, FX, огибающие, маркеры.
Различия сохраняются в отдельную сессию Reaper (добавленное — зелёным, изменённое — жёлтым, удалённое — на дорожках «A ▸ …»)
или, если установлена программа «Reaper to Pro Tools», — сразу в сессию Pro Tools. macOS, Apple Silicon.

Item'ы сопоставляются по IGUID, а если общих GUID нет (например, после конвертации) — по файлу и диапазону источника.

## Установка

Скачайте `ReaperDiff-<версия>.zip` из [Releases](../../releases/latest), распакуйте в «Программы».
При первом запуске: правый клик → «Открыть» или `xattr -cr "/Applications/Reaper Diff.app"`. Нужен `/usr/bin/python3`.

## Командная строка

```sh
python3 engine/rpp_diff.py A.rpp B.rpp --report            # список различий
python3 engine/rpp_diff.py A.rpp B.rpp --out diff.rpp      # сессия различий
python3 engine/rpp_diff.py A.rpp B.rpp --pt ~/Desktop      # сессия Pro Tools (нужен «Reaper to Pro Tools»)
```

## Разработка

```sh
./build.sh                          # сборка и установка в /Applications
release/release.sh 1.2 "что нового" # выпуск (нужны gh и ключ подписи)
```
