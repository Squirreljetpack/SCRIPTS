A few scripts developed for personal use which may be generally useful.

### folder2playlist

Walk a directory, checking the integrity of audio files and collecting valid ones into a playlist.

  - With `--copy`, the files are copied into a provided directory with auto-determined extensions if missing.
  - With `--fix-names <template>`, `mutagen` is used to rename the new files according to metadata.
  - Ex: `./folder2playlist ~/.local/share/osu/files --copy ~/Music/ncmpcpp/osu/ --fix-names --multi` (Create a .m3u playlist from your Osu library).
  - Ex: `./folder2playlist` (Provide arguments interactively).
