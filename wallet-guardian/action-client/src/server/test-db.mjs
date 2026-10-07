import { DatabaseSync } from "node:sqlite";

export function createPrimaryDatabase(schemaSql) {
  const sqlite = new DatabaseSync(":memory:");
  sqlite.exec(schemaSql);
  const session = {
    prepare(sql) {
      const statement = sqlite.prepare(sql);
      const bound = {
        params: [],
        bind(...params) {
          bound.params = params;
          return bound;
        },
        async first() {
          const row = statement.get(...bound.params);
          return row ?? null;
        },
        async all() {
          return { results: statement.all(...bound.params) };
        },
        async run() {
          const info = statement.run(...bound.params);
          return { success: true, meta: { changes: Number(info.changes) } };
        },
      };
      return bound;
    },
    async batch(statements) {
      sqlite.exec("BEGIN IMMEDIATE");
      try {
        const results = [];
        for (const statement of statements) results.push(await statement.run());
        sqlite.exec("COMMIT");
        return results;
      } catch (error) {
        sqlite.exec("ROLLBACK");
        throw error;
      }
    },
  };
  return {
    prepare() {
      throw new Error("authz must use DB.withSession('first-primary')");
    },
    withSession(constraint) {
      if (constraint !== "first-primary") {
        throw new Error(`refusing non-primary D1 session: ${String(constraint)}`);
      }
      return session;
    },
  };
}
