-- Dashboard reads of judge items by claim and of recent judge posts.
CREATE INDEX "idx_judge_item_claim" ON "judge_item"("claim_id");
CREATE INDEX "idx_judge_post_posted" ON "judge_post"("posted_at");
