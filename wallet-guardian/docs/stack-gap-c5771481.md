# !2 tip brought into the !4 stack

`c5771481af7f549c21da058dd8283a7c6582032d` (`fix: stop stale private reads and unsigned SIWE decoys`) is merged with `--no-ff` onto `bd97968063083b810e0b2af33b12d03f249640ea`. That commit is now an ancestor. Nothing was merged to `main`. Send stays closed. Pages was not deployed.

## Conflict choices

- `package.json`: `test:api` runs the !2 client-flow and SIWE gate tests together with the private-task, API, Lista re-read, and `tests/` suites. `test:review` stays.
- `private-task.mjs`: keep the !4 reader. A body over the limit throws, and `finally` cancels the reader and releases the lock. JSON is parsed before that cleanup.
- `main.ts`: keep the Lista re-read page, wallet listeners, and the rule that login and logout do not run at the same time. Before `personal_sign`, `acceptLoginChallenge` must rebuild the message and `validateLoginChallenge` must accept it for the current page origin. Challenge CSRF is stored with `noteAttemptCsrf` and sent only on verify. `beginLogout` drops the displayed plan first; a logout the server does not confirm keeps the session CSRF and says the result is unconfirmed. Cancel succeeds only through `resolveCancel` (`status=cancelled`, same ref and revision, `cancel_allowed=false`, and the denial flags). A timeout says the result is unconfirmed and reads the task again. `requestUserSend` still throws, including when the mainnet flag is on.
- `docs/p1-private-task-api.md`: keep the sentence that a re-read does not open send, and add the !2 notes for logout, rebuilt login messages, cancel, and the global challenge cap.

Server changes from !2 merged without conflicts: expired-challenge cleanup, the global unused-challenge cap, and the shared SIWE message helper.

The review harness cancel fixture now includes `cancel_allowed: false`, matching the server cancel body. The Lista lifecycle preview no longer answers `POST /cancel` with the task document.
