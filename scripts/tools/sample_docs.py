"""A small hand-written knowledge base to ingest into Qdrant (`make ingest`).

The LangGraph and Qdrant entries summarise the page their `url` names, in this repository's own words, so a citation can link to the original
(checked on 2026-11-27). They are long enough to be split into several parent passages with several overlapping child chunks each, which is
what `app/ingestion/chunking.py` does with a real document. The two Ecorp entries are fiction: no page to link, and short enough to stay one
chunk, where a chunk's `text` and `parent_text` are rightly the same.
"""
from typing import NotRequired, TypedDict


class SampleDoc(TypedDict):
    id: int
    topic: str
    title: str
    text: str
    url: NotRequired[str]  # the original document, for the citation's link


def _passages(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs)


DOCS: list[SampleDoc] = [
    {
        "id": 1,
        "topic": "langgraph",
        "title": "LangGraph: Graph API overview",
        "url": "https://docs.langchain.com/oss/python/langgraph/graph-api",
        "text": _passages(
            "LangGraph models an application as a stateful graph built from three things: a state, nodes and edges. The state is the "
            "data the whole graph shares. A node is a function that does some work and returns an update to that state. An edge decides "
            "which node runs next. In short, nodes do the work and edges decide what happens after it. A node can call a language model, "
            "run a tool or do plain computation; to the graph they are all the same kind of thing.",
            "Execution proceeds in discrete steps called super-steps, an idea taken from Google's Pregel system. A node becomes active "
            "when it receives input along one of its incoming edges. Nodes that run in parallel belong to the same super-step, while "
            "nodes that run one after another belong to separate super-steps. The run ends when every node is inactive and no messages "
            "are still in transit between them.",
            "StateGraph is the main class for building a graph, and it is parameterized by a state schema. The schema can be a TypedDict, "
            "a dataclass or a Pydantic model; a TypedDict or a dataclass is the cheaper choice for performance. Each key of the state is "
            "a channel. A node returns only the keys it wants to change, and by default the new value replaces the old one.",
            "A reducer changes how updates to one key are combined. It is a function that takes the current value and the node's update "
            "and returns the new value, and it is attached to the key with typing.Annotated. Using operator.add on a list appends to it "
            "instead of replacing it. The add_messages reducer appends chat messages and updates an existing one when the ids match, and "
            "MessagesState is a ready-made state with a messages key that uses it.",
            "Nodes are registered with add_node, which names a node after its function when no name is given. add_edge draws a fixed "
            "edge, with START and END standing for the entry and the exit of the graph. add_conditional_edges takes a routing function "
            "that returns the next node, END, or a list of nodes to fan out to. Conditional edges are how an agent loops between a "
            "language model node and a tool node until the model stops asking for tools.",
            "A graph must be compiled with compile() before it can be invoked or streamed. Compiling checks the structure, for example "
            "for orphaned nodes, and is where a checkpointer and breakpoints are attached. With a checkpointer, state is saved at each "
            "super-step. After a retry or an interrupt a node runs again from its beginning, so any side effect inside it has to be "
            "safe to repeat.",
        ),
    },
    {
        "id": 2,
        "topic": "langgraph",
        "title": "LangGraph: Checkpointers",
        "url": "https://docs.langchain.com/oss/python/langgraph/checkpointers",
        "text": _passages(
            "A LangGraph checkpointer persists a snapshot of the graph's state at every super-step, grouped into threads. It is what "
            "gives an agent conversation memory across turns, and it also makes possible human-in-the-loop approval, time travel through "
            "earlier states, and recovery after a failure. A graph without a checkpointer forgets everything when a run ends.",
            "A thread is identified by a thread_id, passed inside the configurable part of the run's config. The checkpointer uses "
            "thread_id as its primary key: without one it can neither save state nor resume after an interrupt. Sending new input on an "
            "existing thread does not clear it; the input is merged into the saved state through the reducers and the graph starts again "
            "from its entry point. To begin from an empty state, use a new thread_id.",
            "Each checkpoint is a StateSnapshot holding the channel values, the nodes due to run next, the config, metadata, the creation "
            "time, a link to its parent checkpoint and the pending tasks. get_state returns the latest snapshot of a thread and "
            "get_state_history returns all of them, newest first. update_state writes a new checkpoint and leaves the old one "
            "unchanged; its values pass through the reducers, so a list that appends keeps growing.",
            "Checkpointing also covers partial failure. The writes of nodes that finished inside a super-step are stored even when "
            "another node of the same step fails, so those nodes are not run again on retry. Invoking a thread with no input retries the "
            "unfinished work, and a resume command answers an interrupt, such as a person approving a tool call before it runs.",
            "LangGraph ships several savers. InMemorySaver (older code calls it MemorySaver) keeps checkpoints in process memory and is "
            "meant for experiments, because a restart loses them. SqliteSaver suits local work. PostgresSaver and AsyncPostgresSaver are "
            "the choice for production. An asynchronous graph needs an asynchronous saver. Saved state is serialized with "
            "JsonPlusSerializer by default and can be encrypted with an EncryptedSerializer.",
            "How often a checkpoint reaches storage is the durability mode, chosen per call. With exit, state is saved only when the run "
            "ends. With async, it is written while the next step already runs. With sync, each checkpoint is committed before the next "
            "step starts, which is the safest of the three and the slowest.",
        ),
    },
    {
        "id": 3,
        "topic": "qdrant",
        "title": "Qdrant: Payload",
        "url": "https://qdrant.tech/documentation/manage-data/payload/",
        "text": _passages(
            "Qdrant stores points, each made of vectors and an optional payload. The payload is any information that can be written as "
            "JSON, including nested objects and arrays, attached to the point. It is what lets a search be about more than vector "
            "closeness: an application keeps the text of a passage there, along with who owns it, which document it came from and when "
            "it was written.",
            "Payload values have types that filters understand: integer, float, bool, keyword for strings, geo for a longitude and "
            "latitude pair, datetime in RFC 3339 form, and uuid. A filter condition only looks at values of its own type, so a numeric "
            "range applied to a string matches nothing. When a field holds an array, the condition succeeds if any element of the array "
            "satisfies it. The uuid type stores an identifier in 128 bits instead of a 36 character string.",
            "You can filter a vector search by payload fields, for example restricting results to a single topic or to one customer's "
            "documents. Filtering happens as part of the search, not as a pass over its results afterwards, so asking for ten points "
            "returns the ten closest among those that match the conditions.",
            "A payload is written when a point is upserted and can be changed later without touching the vectors. Setting a payload "
            "updates only the keys you give. Overwriting it replaces the whole object. Individual keys can be deleted, or the payload "
            "cleared altogether. These operations can address points by their ids or by a filter, which makes it possible to relabel "
            "every point that matches a condition in one request.",
            "Filtering stays fast as a collection grows only if the fields you filter on have a payload index. An index is created for "
            "one field with its type. The fields worth indexing are the ones that narrow the result the most, and when several "
            "conditions are combined Qdrant starts from the most restrictive index. An indexed keyword field also supports facet counts, "
            "which report how many points hold each value.",
        ),
    },
    {
        "id": 4,
        "topic": "qdrant",
        "title": "Qdrant: Collections",
        "url": "https://qdrant.tech/documentation/manage-data/collections/",
        "text": _passages(
            "A Qdrant collection is a named set of points that you search among. All the vectors of one kind in a collection share the "
            "same dimensionality and are compared with the same distance metric. The size has to match the output of the embedding "
            "model that produced the vectors. A collection may also define several named vectors, so that one point can hold, say, a "
            "dense and a sparse representation, each with its own size and metric.",
            "Qdrant supports four similarity metrics: Cosine, Dot product, Euclidean distance and Manhattan distance. Which one is right "
            "depends on how the embedding model was trained. Cosine similarity is a common choice for text embeddings. Qdrant implements "
            "it as a dot product over normalized vectors, and normalizes each vector automatically when it is uploaded.",
            "Because vectors from two different embedding models live in unrelated spaces, they cannot be compared even when they happen "
            "to have the same size. Changing the embedding model therefore means computing every vector again, usually into a new "
            "collection built beside the old one while the old one keeps answering queries.",
            "Collection aliases make that switch safe. An alias is another name for a collection that queries can use in its place. "
            "Several alias changes sent in one request are applied atomically, so an application that reads through an alias moves from "
            "the old collection to the new one with no moment where the name points nowhere.",
            "A collection can carry metadata, a set of key and value pairs describing it, such as which embedding model filled it; an "
            "update changes only the keys it names. For many tenants, the usual advice is one collection partitioned by a payload field "
            "rather than a collection per tenant, which fits only a few tenants that need strict isolation.",
        ),
    },
    {
        "id": 5,
        "topic": "company",
        "title": "Ecorp: About",
        "text": "Ecorp was founded in 2021 and builds offline developer tools. "
                "Its flagship product is a local AI stack starter kit.",
    },
    {
        "id": 6,
        "topic": "company",
        "title": "Ecorp: Support Hours",
        "text": "Ecorp's support hours are 9am to 5pm on weekdays, and support is free "
                "for all open-source users.",
    },
]
