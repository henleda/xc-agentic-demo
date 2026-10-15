.PHONY: check policy lint test local-up local-down local-scenarios devbox-up devbox-run devbox-ssh devbox-tunnel devbox-destroy images deploy-aws deploy-vk8s deploy-xc smoke scenarios latency reset

# All Python runs through uv so everyone gets the same interpreter (3.12, see .python-version) and deps.
PY := uv run python

check: policy lint test            ## everything CI runs
policy:
	$(PY) scripts/gen_policy.py --check
lint:
	uv run ruff check services scripts deploy/local
test:
	uv run pytest -q

local-up:                          ## laptop mode, no XC (see deploy/local/README.md)
	$(PY) scripts/gen_keys.py --if-missing
	docker compose -f deploy/local/docker-compose.yaml up -d --build --wait
local-down:
	docker compose -f deploy/local/docker-compose.yaml down -v
local-scenarios:
	$(PY) scripts/run_scenario.py --all --env local

devbox-up:                         ## remote dev server (deploy/devbox/README.md)
	deploy/devbox/devbox.sh up
devbox-run:                        ## e.g. make devbox-run CMD="make local-up"
	deploy/devbox/devbox.sh run '$(CMD)'
devbox-ssh:
	deploy/devbox/devbox.sh ssh
devbox-tunnel:
	deploy/devbox/devbox.sh tunnel
devbox-destroy:
	deploy/devbox/devbox.sh destroy

images:                            ## multi-arch build + push; verifies arbitrary-UID run
	scripts/build_images.sh
deploy-aws:
	cd deploy/aws && terraform init && terraform apply
deploy-vk8s:
	$(PY) scripts/render_secrets.py && $(PY) scripts/render_manifests.py && \
	kubectl --kubeconfig $$HOME/.kube/agentic-demo.yaml apply -f deploy/vk8s/secrets/rendered/ && \
	kubectl --kubeconfig $$HOME/.kube/agentic-demo.yaml apply -f deploy/vk8s/rendered/
deploy-xc:
	$(PY) scripts/gen_policy.py && cd deploy/xc/terraform && terraform init && terraform apply

smoke:
	$(PY) scripts/smoke_test.py --env tenant
scenarios:
	$(PY) scripts/run_scenario.py --all --env tenant
latency:
	$(PY) scripts/measure_latency.py --env tenant
reset:
	scripts/reset_demo.sh
