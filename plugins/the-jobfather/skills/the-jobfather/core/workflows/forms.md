# Form answers

Answer in the original order and numbering.

- **Factual fields** get one line with only the value: a title field gets the title, not the
  employer, dates or location. Use `python3 SKILL/scripts/facts.py FIELD` (`current_job_title`,
  `current_employer`, `full_name`, `email`, `phone`, `location`, any HR-DETAILS key such as
  `notice_period`, `custom.<name>`, or `"answer:<question text>"` for saved form answers). No JD is
  needed. A null value is unknown: ask for just that value, for example "What is your current
  notice period?", without explaining storage.
- **Descriptive answers:** one relevant evidence example, 2-3 short sentences unless the question
  asks for more. Use the application's analysis when helpful.
- **Mixed batches:** answer everything known, then ask only for the missing values.
- Never reuse an old pay or availability value without checking it is still current; never infer
  relocation, availability or authorization from the job.
- Self-identification (gender, ethnicity, disability, veteran status) uses the user's saved choice;
  the default is "prefer not to say".
- Respect each field's own limit (words or characters, with or without spaces). Run
  `check_text.py` on each final answer, not on question labels. Shorten only the answer that fails.
- Save the ordered batch as one `forms` output (workspace.md). When the user confirms a new answer
  they will reuse, queue it for FORM-ANSWERS.json (learn.md).
