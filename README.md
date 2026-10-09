# DS916Tray — tbrggreen Edition

**English | [Русский](#русский)**

An independent community modification of [DS916 Sensor Panel](https://github.com/mike-novotny/sensor-panel) for the Jonsbo DS916 USB display. This version adds a Windows tray interface, automatic Gaming/Work/Idle modes, Russian localization, importable language dictionaries, and Windows media-session controls.

> Maintainer: **tbrggreen**. This is an independent community modification, not an official release by the upstream author or by Jonsbo.

## Features

- Portrait (462 × 1920) and landscape (1920 × 462) display orientations.
- HWiNFO shared-memory sensor integration for hardware metrics.
- Optional RTSS FPS/frametime data and automatic Gaming-mode detection.
- Configurable GPU-load threshold for automatic mode switching.
- Windows tray menu, settings, and localized DS916 Status window.
- Built-in English/Russian UI and importable JSON language dictionaries.
- User-loadable themes and a Theme Builder integration when `theme_builder.html` is installed.
- Windows media-session integration: track title, artist, cover art, progress, and playback controls for compatible players.
- Included example skin: **tbrggreen — 3 Screens**, based on the author's supplied theme.
- A custom green-accented Jonsbo DS916 application icon for the executable, desktop shortcut, and tray, created for this project by `tbrggreen`.

## Requirements

- Windows 10 or Windows 11, 64-bit.
- Python 3.10 or newer for running from source/building.
- Jonsbo DS916 display connected over USB and its COM port available.
- HWiNFO64 for hardware sensor readings.
- RivaTuner Statistics Server (RTSS), usually installed with MSI Afterburner, for FPS/frametime. This is optional; the rest of the app can run without it.
- A media player that publishes a Windows media session for the built-in player integration. Spotify for Windows is a common example; no Spotify API key or separate Spotify SDK is required for the GSMTC integration. Compatibility depends on the player and how it exposes its session to Windows.

## Install and run from source

1. Install Python from <https://www.python.org/downloads/>. During setup, enable **Add Python to PATH**.
2. Download/clone this repository and open PowerShell in the repository folder.
3. Install the Python packages:

   ```powershell
   py -m pip install -r requirements.txt
   ```

4. Run the app:

   ```powershell
   py ds916_tray.py
   ```

5. Right-click the tray icon and open **Settings** to select the COM port, configure HWiNFO/RTSS, and choose the display orientation/theme.

## Build a Windows executable

Double-click `build.bat` or run it from a Command Prompt. It installs the dependencies, builds `dist\DS916Tray.exe` with the custom icon, copies the example theme to `%APPDATA%\DS916Tray\Themes\` if it is not already present, and creates a desktop shortcut. If you have a `theme_builder.html` file, place it in the repository root before building; the script copies it to `%APPDATA%\DS916Tray\`.

The build script is a convenience for Windows and should be tested on the target PC before publishing a release. Do not commit generated `build/`, `dist/`, or user configuration files.

## Player integration: installation and verification

The player uses **Windows Global System Media Transport Controls (GSMTC)** via the `winsdk` Python package. It does not call the Spotify Web API. The player depends on a compatible media session being exposed by Windows.

1. Install the Python requirements with `py -m pip install -r requirements.txt`. On Windows, this installs `winsdk` for media-session access.
2. Start a compatible media player, for example Spotify for Windows, and sign in if required.
3. Start playback of a track. Keep the player running while testing.
4. Launch DS916Tray. Open Settings and select/configure the player display in the theme; then start the display.
5. Check that the display shows the track title and artist, and that album art/progress appear when the media session provides them.
6. Test Play/Pause, Previous, and Next controls. These controls work only when the active player/session supports the relevant commands.
7. If the player data does not appear, confirm playback is active, check Windows media controls/volume flyout for the track, close and reopen the player, and restart DS916Tray. Some browser players or third-party apps may not expose all metadata or controls.

No separate Spotify API key, client ID, or redirect URL is required for this GSMTC-based integration. Internet access is needed by Spotify itself, not by DS916Tray to read the Windows media session.

## Hardware sensors: quick check

1. Install and run HWiNFO64.
2. In HWiNFO settings, enable **Shared Memory Support** (Settings → General → Shared Memory Support; wording can vary by version).
3. Leave HWiNFO running, open DS916Tray's **Status…** window, and click **Refresh**.
4. Confirm the sensor source reports a connected/available state and sensor values update. Use **Discover Sensors** if the sensor mapping needs refreshing.

## FPS/frametime: quick check (optional)

1. Install MSI Afterburner with RivaTuner Statistics Server (RTSS) from the official MSI/RTSS distribution.
2. Start RTSS and launch a 3D game/application.
3. Confirm that RTSS is running and FPS is available; open DS916Tray's Status window and refresh it.
4. If FPS is missing, verify RTSS is running and that the game is detected by RTSS. The app can still be used without FPS integration.

## Included example theme

`Themes/tbrggreen_3_Screens.ds916theme` is the three-screen theme supplied by the project owner. It contains separate layouts for Gaming, Work, and Idle modes. Import it with the app's **Load Theme…** menu item. The theme file is also copied into `%APPDATA%\DS916Tray\Themes\` by `build.bat` if no file with that name exists yet.

## Language dictionaries and settings

The app's configuration and imported themes/dictionaries are stored under `%APPDATA%\DS916Tray\`. Imported language dictionaries go in the `Languages` subfolder; themes go in `Themes`. The starter dictionary template is in `Languages/language_template.json`. Keep dictionary keys unchanged and translate values only.

## Attribution and license

This repository is derived from [mike-novotny/sensor-panel](https://github.com/mike-novotny/sensor-panel). The upstream project identifies itself as MIT-licensed. Before redistributing, include the upstream `LICENSE` file verbatim and preserve its copyright notice. Do not replace the upstream license with a newly written notice. This repository's own changes are not an official upstream release; attribution does not imply endorsement.

The application icon was created for this project by `tbrggreen`. Jonsbo is a third-party trademark; its mention does not imply affiliation or endorsement.

---

## Русский

**DS916Tray — tbrggreen Edition** — независимая модификация [DS916 Sensor Panel](https://github.com/mike-novotny/sensor-panel) для USB-дисплея Jonsbo DS916. Добавлены управление через системный трей, автоматические режимы Gaming/Work/Idle, русский интерфейс, пользовательские словари и управление медиасеансами Windows.

> Автор и сопровождающий проекта: **tbrggreen**. Это независимая модификация, не являющаяся официальным релизом автора исходного проекта или Jonsbo.

### Возможности

- Вертикальная ориентация 462 × 1920 и горизонтальная 1920 × 462.
- Показания датчиков через общую память HWiNFO64.
- Необязательные FPS/frametime из RTSS и автоматическое определение игрового режима.
- Настраиваемый порог загрузки GPU для переключения в Gaming.
- Меню в трее, настройки и русифицированное окно DS916 Status.
- Встроенные английский и русский языки, импорт пользовательских JSON-словарей.
- Загрузка тем; интеграция с редактором тем при наличии `theme_builder.html`.
- Информация о треке, исполнителе, обложке и прогрессе, а также команды управления воспроизведением через медиасеансы Windows.
- Пример скина **tbrggreen — 3 экрана**, приложенный владельцем проекта.
- Собственная зелёная акцентная иконка для `.exe`, ярлыка рабочего стола и трея, созданная `tbrggreen`.

### Требования

- Windows 10/11, 64-разрядная версия.
- Python 3.10 или новее для запуска исходников и сборки.
- Дисплей Jonsbo DS916, подключённый по USB, и доступный COM-порт.
- HWiNFO64 для датчиков системы.
- RivaTuner Statistics Server (RTSS), обычно устанавливаемый вместе с MSI Afterburner, для FPS/frametime. Необязателен.
- Медиаплеер, который публикует медиасеанс Windows. Например, Spotify для Windows. Совместимость зависит от того, какие данные и команды плеер передаёт в Windows.

### Установка из исходников

1. Установи Python с <https://www.python.org/downloads/>. В установщике включи **Add Python to PATH**.
2. Скачай репозиторий и открой PowerShell в его папке.
3. Установи зависимости:

   ```powershell
   py -m pip install -r requirements.txt
   ```

4. Запусти приложение:

   ```powershell
   py ds916_tray.py
   ```

5. Нажми правой кнопкой по иконке в трее, открой настройки и выбери COM-порт, настрой источники датчиков и тему.

### Сборка `.exe`

Запусти `build.bat` двойным щелчком или из командной строки. Скрипт установит зависимости, соберёт `dist\DS916Tray.exe` с новой иконкой, скопирует пример темы в `%APPDATA%\DS916Tray\Themes\`, если его там ещё нет, и создаст ярлык на рабочем столе. Чтобы установить редактор тем, положи `theme_builder.html` в корень репозитория перед сборкой.

### Плеер: установка и проверка

Плеер использует **Windows GSMTC (Global System Media Transport Controls)** через пакет Python `winsdk`. Он не обращается к Spotify Web API, поэтому отдельный Spotify API-ключ, Client ID или Redirect URL не нужны.

1. Выполни `py -m pip install -r requirements.txt` — в Windows установится `winsdk`.
2. Запусти совместимый плеер, например Spotify для Windows, и войди в аккаунт, если это требуется.
3. Включи любой трек и оставь плеер работающим.
4. Запусти DS916Tray и активируй тему с элементами плеера.
5. Проверь, что появились название трека и исполнитель; обложка и прогресс отображаются, если плеер передаёт их в медиасеансе.
6. Проверь кнопки Play/Pause, Previous и Next. Поддержка зависит от активного медиасеанса.
7. Если данных нет, проверь, отображается ли трек в системном медиа-виджете Windows, перезапусти плеер и DS916Tray. Некоторые браузерные плееры и приложения не передают все метаданные или команды.

### Проверка HWiNFO64

1. Установи и запусти HWiNFO64.
2. В настройках HWiNFO включи **Shared Memory Support** (обычно Settings → General → Shared Memory Support; название может немного отличаться в разных версиях).
3. Оставь HWiNFO работающим, открой в DS916Tray окно «Состояние…» и нажми «Обновить».
4. Убедись, что источник датчиков доступен и показания обновляются. Если нужно, используй «Найти датчики».

### Проверка FPS через RTSS (необязательно)

1. Установи MSI Afterburner вместе с RivaTuner Statistics Server.
2. Запусти RTSS и 3D-игру.
3. Проверь, что RTSS работает и видит игру, затем обнови окно «Состояние…» в DS916Tray.
4. Если FPS не появляется, проверь обнаружение игры в RTSS. Остальные функции приложения работают и без RTSS.

### Пример темы на три экрана

Файл `Themes/tbrggreen_3_Screens.ds916theme` — текущий скин, приложенный владельцем проекта. В нём есть отдельные макеты для Gaming, Work и Idle. Загрузи его через пункт меню **«Загрузить тему…»**. `build.bat` копирует тему в `%APPDATA%\DS916Tray\Themes\`, если там ещё нет файла с таким именем.

### Словари и настройки

Настройки, темы и импортированные словари приложения хранятся в `%APPDATA%\DS916Tray\`. Темы находятся в `Themes`, словари — в `Languages`. Шаблон словаря: `Languages/language_template.json`. Не меняй ключи JSON — переводи только значения.

### Авторство и лицензия

Проект основан на [mike-novotny/sensor-panel](https://github.com/mike-novotny/sensor-panel), в котором указана лицензия MIT. Перед распространением добавь оригинальный файл `LICENSE` из исходного репозитория без изменений и сохрани уведомление об авторских правах. Не заменяй его новым текстом. Эта модификация не является официальным релизом исходного автора.

Иконка приложения создана `tbrggreen` специально для этого проекта. Jonsbo — сторонний товарный знак; его упоминание не означает аффилированность или одобрение со стороны владельца бренда.
