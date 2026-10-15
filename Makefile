.PHONY: check policy lint test local-up local-down images deploy-aws deploy-vk8s deploy-xc smoke scenarios latency reset

check: policy lint test            ## everything CI runs
policy:
	python scripts/gen_policy.py --check
lint:
	uv run ruff check services scripts
test:
	uv run pytest -q

local-up:
	docker compose -f deploy/local/docker-compose.yaml --env-file .env up -d --build
local-down:
	docker compose -f deploy/local/docker-compose.yaml down -v

images:                            ## multi-arch build + push; verifies arbitrary-UID run
	scripts/build_images.sh
deploy-aws:
	cd deploy/aws && terraform init && terraform apply
deploy-vk8s:
	python scripts/render_secrets.py && python scripts/render_manifests.py && \
	kubectl --kubeconfig $$HOME/.kube/agentic-demo.yaml apply -f deploy/vk8s/secrets/rendered/ && \
	kubectl --kubeconfig $$HOME/.kube/agentic-demo.yaml apply -f deploy/vk8s/rendered/
deploy-xc:
	python scripts/gen_policy.py && cd deploy/xc/terraform && terraform init && terraform apply

smoke:
	python scripts/smoke_test.py --env tenant
scenarios:
	python scripts/run_scenario.py --all --env tenant
latency:
	python scripts/measure_latency.py --env tenant
reset:
	scripts/reset_demo.sh
