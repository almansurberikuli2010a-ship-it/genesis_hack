PY ?= python3.11

check:          ## единая проверка (офлайн)
	$(PY) -m auditgym check
jury:           ## репетиция оценки жюри (раздел 10)
	$(PY) -m auditgym jury
test:
	$(PY) -m unittest discover -s tests -t .
mutation:       ## мутационный тест самой проверки (~2 мин)
	$(PY) scripts/mutation_test.py
export:         ## пересобрать episodes/ (train + dev)
	$(PY) -m auditgym export
trajectories:   ## пересобрать trajectories/trajectories.jsonl
	$(PY) -m auditgym trajectories
demo:
	$(PY) -m auditgym demo
docker-check:   ## сборка образа и проверка без сети
	docker build -t auditgym . && docker run --rm --network none --cpus 2 --memory 4g auditgym
.PHONY: check jury test mutation export trajectories demo docker-check
