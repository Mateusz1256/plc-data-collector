# Task 12: Driver OPC UA

## Cel

Dodać pierwszy rzeczywisty driver protokołu.

## Zakres

- klient async OPC UA,
- connect/disconnect,
- batch read,
- source timestamp,
- quality mapping,
- timeouty,
- reconnect,
- konfiguracja certyfikatów bez przechowywania sekretów w repo,
- testy z lokalnym serwerem lub kontrolowanym fixture.

## Poza zakresem

- subskrypcje,
- write,
- discovery,
- pełne zarządzanie PKI,
- redundancy.

## Kryteria akceptacji

- driver spełnia wspólny kontrakt,
- czyta wiele node'ów,
- mapuje błędy pojedynczych tagów,
- po zerwaniu sesji może się połączyć ponownie,
- testy integracyjne są oznaczone osobnym markerem.

## Licencje

Przed dodaniem biblioteki:

- zweryfikuj dokładną licencję użytej wersji,
- uzupełnij `THIRD_PARTY_NOTICES.md`,
- zachowaj wymagane notice files.

## Proponowany commit

```text
feat(opcua): add asynchronous batch read driver
```
