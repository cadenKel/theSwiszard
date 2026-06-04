# swiszCLI systemd integration

## Install dream_cycle nightly timer (4am)

    mkdir -p ~/.config/systemd/user
    cp contrib/systemd/swiszcli-dream.* ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now swiszcli-dream.timer
    systemctl --user list-timers swiszcli-dream.timer

## Run it now (manual)

    swiszcli-dream --dry-run
    swiszcli-dream
    swiszcli-dream --json

## Config (~/.swiszcli/dream_cycle.toml)

    promote_threshold = 5
    prune_days        = 30
    dep_min_losses    = 3
    dep_loss_ratio    = 2.0
    swizmem_url       = "http://127.0.0.1:7437"

Logs: ~/.swiszcli/dream_cycle.log (JSON per line)
