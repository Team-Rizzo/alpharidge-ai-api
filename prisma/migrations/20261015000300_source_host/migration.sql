-- Sources are keyed by host within their registrable domain.
ALTER TABLE "source" ADD COLUMN "host" TEXT NOT NULL DEFAULT '';
UPDATE "source" SET "host" = "domain" WHERE "host" = '';
DROP INDEX "source_domain_path_rule_key";
CREATE UNIQUE INDEX "source_domain_host_path_rule_key" ON "source"("domain", "host", "path_rule");
