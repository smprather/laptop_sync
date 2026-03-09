## Project Overview

- I need a tool that will mirror files from a Windows source directory (recursive) to a Linux destination directory.
- You must only use scp to copy data, and ssh to execute commands on the Linux host.
- You must only copy files when the contents of the source directory changes, but the first iteration of the
  polling loop must check for consistency.
- Only copy the files that are need to be copied.
- This is a mirror operation, so if a file in the source directory is deleted, it needs to be deleted at the
  destination.
- Assume passwordless ssh is already set up.
- Use a yaml file for configuration. Any config on command line overrides the yaml file. Default yaml config file is
  laptop_sync.yaml.
- When comparing files, you only need to compare modification date and file size. Make sure to preserve
  modification times when copying to the destination to prevent an update loop.

## Tech Stack

- Astral uv
- rich-click
- python3.14
- pyyaml
