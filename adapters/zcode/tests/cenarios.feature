Feature: validação pública do worker complementar

  # test: tests/test_gate_extra.py::test_cli_foreground_stdin_json
  Scenario: tarefa por stdin produz JSON estruturado
    Given um contexto fake local
    When a tarefa é enviada pela entrada padrão
    Then a resposta contém status ok e seleção efetiva

  # test: tests/test_gate_extra.py::test_cli_rejects_effective_selection_mismatch
  Scenario: seleção divergente é rejeitada
    Given o CLI fake devolve modelo diferente
    When a resposta é validada
    Then o processo termina com erro seguro

  # test: tests/test_gate_extra.py::test_cli_dry_run_doctor_and_argument_policy
  Scenario: diagnóstico e dry-run não executam modelo
    Given o worker está em staging
    When doctor e dry-run são chamados
    Then os JSONs públicos permanecem válidos

  # test: tests/test_gate_extra.py::test_cli_timeout_and_output_limit_are_safe
  Scenario: timeout e limite de saída são finitos
    Given o CLI fake dorme ou emite saída excessiva
    When o prazo ou limite é alcançado
    Then o worker encerra sem vazar stderr bruto

  # test: tests/test_gate_extra.py::test_adversarial_attachments_denylist_and_json_are_rejected
  Scenario: entradas adversariais são recusadas
    Given há anexo secreto, symlink e JSON ambíguo
    When os validadores recebem as entradas
    Then cada entrada é rejeitada

  # test: tests/test_gate_extra.py::test_private_background_job_status_cancel_and_cleanup
  Scenario: job privado pode ser cancelado
    Given um job local está executando
    When o status e o cancelamento são consultados
    Then o input privado é removido após o terminal
