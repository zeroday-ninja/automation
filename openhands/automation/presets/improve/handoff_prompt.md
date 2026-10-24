# Your runtime is nearly out of usage

Stop the work. Do not research anything else, do not run the test suite, do
not start anything new. What is left of this runtime is for recording where
you got to, so another runtime can continue instead of starting again.

Do these four things, in this order. The order matters: the code is worth more
than the description of it, so it is secured first.

1. **Push what you have.**
   `git add -A && git commit -m "wip: handing off" && git push -u origin {{BRANCH}}`
   An unpushed branch cannot be seen by the next cycle, which runs in a new
   sandbox. If the push fails, say so in step 3 and carry on.

2. **Write `.improve-cycle/handoff.md`**, under 500 words:
   - what you were trying to do, in one or two sentences;
   - what is done and verified;
   - what is next, concretely enough to act on without re-deriving it;
   - **what you tried that did not work, and why.** This is the part that
     saves the next runtime its whole budget. Be specific: the approach, what
     happened, and what would have to be different for it to work.

3. **Write `.improve-cycle/handoff.json`** to exactly this shape:

```json
{
  "status": "incomplete",
  "reason": "runtime_exhausted",
  "branch": "{{BRANCH}}",
  "branch_pushed": true,
  "head_sha": "the commit you just pushed",
  "stage": "research | implementing | verifying | publishing",
  "goal": "the one improvement, in under 600 characters",
  "done": ["what is finished and verified"],
  "next_steps": ["the next concrete action", "and the one after"],
  "do_not": ["an approach you tried and rejected, and why"],
  "files_touched": ["paths you changed"]
}
```

`next_steps` must not be empty. A handoff with no next step is
indistinguishable from starting over, and will be discarded.

4. **Commit and push those two files.** Then stop. Do not do anything else.
