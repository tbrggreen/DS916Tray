# Changelog

## v35.0.0 - 2026-10-09

### Added
- Added a configurable RTSS FPS threshold for automatic Gaming mode activation.
- Added a slider to configure the FPS threshold from 1 to 120 FPS.
- Gaming mode now activates when either the GPU load threshold or the RTSS FPS threshold is reached.

### Changed
- Set the default RTSS FPS threshold to 45 FPS.
- Preserved the existing GPU load threshold setting.

## v34.0.0 — repository release preparation

- Updated the packaged application to the working v34 source.
- Made the weather header city name follow the city selected in General settings.
- Included the dynamic-city three-screen theme for Gaming, Work, and Idle modes.
- Added the custom Jonsbo DS916 application icon for the executable, desktop shortcut, and system tray.
- Standardized project branding and theme filenames around the author handle `tbrggreen`.
- Cleaned the repository package and excluded Python cache/build artifacts from the release archive.
- Updated bilingual README instructions and build script references.

## v31.0.0 — repository preparation

- Prepared the initial public-repository structure.
- Added `build.bat` to install dependencies, package the app with PyInstaller, copy the example theme when missing, and create a desktop shortcut.
- Expanded English and Russian documentation with player requirements, installation steps, and verification checklists for GSMTC, HWiNFO64, and optional RTSS.
- Documented that Windows media-session integration does not require a Spotify API key.

## Previous work

- Russian and English UI localization with importable JSON language dictionaries.
- Tray menu and DS916 Status localization.
- Automatic Gaming/Work/Idle display modes and configurable GPU threshold.
- Orientation-specific themes, theme loading, settings, and Windows media-session player.
