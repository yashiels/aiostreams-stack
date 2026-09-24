HOST ?= user@host
REMOTE ?= ~/apps/media-gateway
PROJECT ?= media-gateway
SSH_OPTS ?= -o StrictHostKeyChecking=yes
SMOKE_DIR ?= .smoke
SMOKE_PORT ?= 38080
RSYNC_EXCLUDES = --exclude '.env' --exclude 'data/' --exclude '.git' \
	--exclude 'restore-rollback/' --exclude 'restore.*/' --exclude '__pycache__/' --exclude '.stack.lock'
COMPOSE = docker compose -p $(PROJECT) --env-file .env --env-file versions.env -f compose.yml

export SMOKE_DIR SMOKE_PORT

.PHONY: deploy deploy-prep sync env probe bootstrap bootstrap-dry verify logs rollback backup restore-check smoke-env smoke-up smoke-down

bootstrap-dry:
	python3 ops/bootstrap.py --dry-run

bootstrap:
	python3 ops/bootstrap.py

deploy: sync
	ssh $(SSH_OPTS) $(HOST) 'PROJECT=$(PROJECT) EXPECTED_COMPOSE_SHA256=$(EXPECTED_COMPOSE_SHA256) bash $(REMOTE)/ops/deploy.sh'

deploy-prep: sync
	ssh $(SSH_OPTS) $(HOST) 'PROJECT=$(PROJECT) EXPECTED_COMPOSE_SHA256=$(EXPECTED_COMPOSE_SHA256) bash $(REMOTE)/ops/deploy.sh --no-tunnel'

sync:
	rsync -az --delete $(RSYNC_EXCLUDES) -e 'ssh $(SSH_OPTS)' ./ $(HOST):$(REMOTE)/

env:
	./ops/push-env.sh $(HOST) '$(REMOTE)'

probe:
	ssh $(SSH_OPTS) $(HOST) 'PROJECT=$(PROJECT) bash $(REMOTE)/ops/probe.sh --verbose'

verify:
	python3 ops/verify.py --ssh-host '$(HOST)' --remote-dir '$(REMOTE)' --project '$(PROJECT)'

logs:
	ssh $(SSH_OPTS) $(HOST) 'cd $(REMOTE) && $(COMPOSE) logs --tail=200'

rollback:
	@test -n "$(REF)" || { echo "usage: make rollback REF=<git-ref>"; exit 1; }
	git checkout $(REF) -- versions.env
	$(MAKE) deploy

backup:
	ssh $(SSH_OPTS) $(HOST) 'PROJECT=$(PROJECT) bash $(REMOTE)/backup/backup.sh'

restore-check:
	ssh $(SSH_OPTS) $(HOST) 'cd $(REMOTE) && eval "$$(./ops/env-export.py .env)" && restic snapshots --tag media-gateway'

smoke-env:
	./ops/smoke.sh env "$$SMOKE_DIR" "$$SMOKE_PORT"

smoke-up:
	./ops/smoke.sh up "$$SMOKE_DIR" "$$SMOKE_PORT"

smoke-down:
	./ops/smoke.sh down "$$SMOKE_DIR" "$$SMOKE_PORT"
