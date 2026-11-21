-- Synthetic PG-only E5 corpus. Run only in the benchmark's dedicated database.
CREATE TEMP TABLE e5_items AS
SELECT i, hashint4(i) & 2147483647 AS shape,
       CASE WHEN i <= 192 THEN (i-1) % 8
            WHEN i % 500 = 0 THEN (i / 500) % 2
            ELSE 8 + (i-193) / 48 END AS cohort,
       md5('e5-memory:' || i)::uuid AS memory_id
FROM generate_series(1, current_setting('e5.count')::int) AS g(i);

CREATE TEMP TABLE e5_scopes AS
SELECT DISTINCT cohort, 'e5-project-' || cohort AS project,
       'example/e5-repo-' || cohort AS repository, 'e5-owner-' || (cohort % 3) AS actor,
       md5('e5-thread:' || cohort)::uuid AS thread_id,
       md5('e5-mandate:' || cohort)::uuid AS mandate_id,
       md5('e5-ticket:' || cohort)::uuid AS ticket_id,
       md5('e5-run:' || cohort)::uuid AS run_id
FROM e5_items;
ALTER TABLE e5_scopes ADD PRIMARY KEY(cohort);

CREATE TEMP TABLE e5_versions AS
SELECT cohort, slot,
       left(md5('e5-version:' || cohort || ':' || slot) || md5('e5-tail:' || cohort),40) AS sha
FROM e5_scopes CROSS JOIN generate_series(0,1) AS releases(slot);
ALTER TABLE e5_versions ADD PRIMARY KEY(cohort,slot);

INSERT INTO app.threads(thread_id,project_id,repository,external_key)
SELECT thread_id,project,repository,'e5-synthetic' FROM e5_scopes;
INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,execution_mode)
SELECT mandate_id,project,actor,'{}','r1-explicit-command-v1','fixture' FROM e5_scopes;
INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status,outcome)
SELECT ticket_id,mandate_id,thread_id,'ended','succeeded' FROM e5_scopes;
INSERT INTO app.runs(run_id,ticket_id,attempt_no,input_revision,graph_version,execution_mode,
                     status,question)
SELECT run_id,ticket_id,1,1,'r1-quotes-v1','fixture','succeeded','E5 frozen synthetic dataset'
FROM e5_scopes;

INSERT INTO app.artifacts(content_hash,byte_count)
VALUES (current_setting('e5.artifact'), current_setting('e5.artifact_bytes')::bigint);

CREATE TEMP TABLE e5_sources AS
SELECT v.cohort,v.slot,
       array_agg(md5('e5-proof:' || v.cohort || ':' || slot || ':' || p)::uuid ORDER BY p)
           AS evidence_ids
FROM e5_versions v CROSS JOIN generate_series(1,24) AS proofs(p)
GROUP BY v.cohort,v.slot;
ALTER TABLE e5_sources ADD PRIMARY KEY(cohort,slot);

INSERT INTO app.evidence(evidence_id,run_id,project_id,source_uri,source_version,content_hash,
                         start_line,end_line,tool_call_id)
SELECT id,sc.run_id,sc.project,
       'synthetic://' || sc.repository || '@' || v.sha || '/配置/证据/检索/notes-' || line,
       v.sha,current_setting('e5.artifact'),line,line,id
FROM e5_sources p JOIN e5_scopes sc USING(cohort) JOIN e5_versions v USING(cohort,slot)
CROSS JOIN unnest(p.evidence_ids) WITH ORDINALITY AS e(id,line);

CREATE TEMP TABLE e5_details AS
SELECT i.*,sc.project,sc.repository,sc.actor,sc.ticket_id,sc.run_id,v.sha,
       CASE WHEN shape % 100 < 50 THEN 'semantic'
            WHEN shape % 100 < 75 THEN 'episodic'
            WHEN shape % 100 < 85 THEN 'procedural' ELSE 'working' END AS kind,
       CASE WHEN (shape / 100) % 100 < 58 THEN 'commit'
            WHEN (shape / 100) % 100 < 83 THEN 'repository' ELSE 'project' END AS scope,
       CASE WHEN (shape / 10000) % 100 < 8 THEN 'stale'
            WHEN (shape / 10000) % 100 < 14 THEN 'revoked' ELSE 'active' END AS status,
       evidence_ids[1:CASE WHEN shape % 100 BETWEEN 50 AND 74 AND shape / 17 % 3 = 0 THEN 24
                           WHEN shape / 17 % 7 = 0 THEN 0
                           ELSE shape / 17 % 4 + 1 END] AS evidence_ids,
       timestamptz '2026-11-19 00:00:00+00' - make_interval(mins=>i % 43200) AS created,
       (ARRAY['配置验证','日志检查','缓存边界','任务恢复','证据版本','数据导入',
              '权限审查','测试回执'])[1+shape % 8] AS topic
FROM e5_items i JOIN e5_scopes sc USING(cohort)
JOIN e5_versions v ON v.cohort=i.cohort AND v.slot=CASE WHEN shape / 13 % 5=0 THEN 1 ELSE 0 END
JOIN e5_sources p ON (p.cohort,p.slot)=(v.cohort,v.slot);

INSERT INTO knowledge.memory_records(memory_id,memory_kind,project_id,repository,commit_sha,actor,
    scope_kind,scope_key,subject_key,summary_hash,evidence_ids,artifact_hashes,confidence,
    verification_status,status,created_at,updated_at,stale_at,stale_reason,revoked_at,revoked_reason)
SELECT memory_id,kind,project,repository,CASE WHEN scope='commit' THEN sha END,actor,
       scope,CASE scope WHEN 'project' THEN project WHEN 'repository' THEN repository ELSE sha END,
       'e5/' || lpad(i::text,7,'0'),current_setting('e5.artifact'),to_jsonb(evidence_ids),
       jsonb_build_array(current_setting('e5.artifact')),0.50 + (shape % 50) / 100.0,
       CASE WHEN cardinality(evidence_ids)>0 THEN 'verified' ELSE 'supported' END,
       status,created,created, CASE WHEN status='stale' THEN created END,
       CASE WHEN status='stale' THEN 'synthetic-e5' END,
       CASE WHEN status='revoked' THEN created END,
       CASE WHEN status='revoked' THEN 'synthetic-e5' END
FROM e5_details;

INSERT INTO knowledge.memory_semantic(memory_id,subject,predicate,object_value)
SELECT memory_id,topic,'observation',
       repeat(topic || ' 配置变更与检索记录已在当前版本完成复核。相关证据说明请求边界、日志与预期数据。'
             || ' 回归验证覆盖任务状态和缓存读取范围，提供核对所需的时间与来源。',6+shape / 19 % 6)
FROM e5_details WHERE kind='semantic';
INSERT INTO knowledge.memory_episodic(memory_id,ticket_id,run_id,event_kind,occurred_at)
SELECT memory_id,ticket_id,run_id,'e5-investigation-' || shape % 16,created
FROM e5_details WHERE kind='episodic';
INSERT INTO knowledge.memory_procedural(memory_id,task_key,recipe_hash,success_count,failure_count)
SELECT memory_id,'e5-verification-' || shape % 32,current_setting('e5.artifact'),shape % 9,shape % 3
FROM e5_details WHERE kind='procedural';
INSERT INTO knowledge.memory_working(memory_id,run_id,expires_at)
SELECT memory_id,run_id,CASE WHEN shape / 37 % 4=0 THEN timestamptz '2040-01-01T00:00Z'
                            ELSE timestamptz '2020-01-01T00:00Z' END
FROM e5_details WHERE kind='working';
