import sys
import pickle
import time
import numpy as np
import torch.cuda

def list_rules(attn_ops, attn_mems, the):
    """
    Given attentions over operators and memories, 
    enumerate all rules and compute the weights for each.
    
    Args:
        attn_ops: a list of num_step vectors, 
                  each vector of length num_operator.
        attn_mems: a list of num_step vectors,
                   with length from 1 to num_step.
        the: early prune by keeping rules with weights > the
    
    Returns:
        a list of (rules, weight) tuples.
        rules is a list of operator ids. 
    
    """
    
    num_step = len(attn_ops)
    paths = {t+1: [] for t in range(num_step)}
    paths[0] = [([], 1.)]
    for t in range(num_step):
        # print ("T = " + str(t))
        for m, attn_mem in enumerate(attn_mems[t]):
            for p, w in paths[m]:
                paths[t+1].append((p, w * attn_mem))
        if t < num_step - 1:
            new_paths = []           
            for o, attn_op in enumerate(attn_ops[t]):
                for p, w in paths[t+1]:
                    if w * attn_op > the:
                        new_paths.append((p + [o], w * attn_op))
            paths[t+1] = new_paths
    this_the = min([the], max([w for (_, w) in paths[num_step]]))
    final_paths = filter(lambda x: x[1] >= this_the, paths[num_step])
    final_paths_sorted = sorted(final_paths, key=lambda x: x[1], reverse=True)
    
    return final_paths_sorted


def print_rules(q_id, rules, parser, query_is_language):
    """
    Print rules by replacing operator ids with operator names
    and formatting as logic rules.
    
    Args:
        q_id: the query id (the head)
        rules: a list of ([operator ids], weight) (the body)
        parser: a dictionary that convert q_id and operator_id to 
                corresponding names
    
    Returns:
        a list of strings, each string is a printed rule
    """
    
    if len(rules) == 0:
        return []
    
    if not query_is_language: 
        query = parser["query"][q_id]
    else:
        query = parser["query"](q_id)
        
    # assume rules are sorted from high to lows
    max_w = rules[0][1]
    # compute normalized weights also    
    rules = [[rule[0], rule[1], rule[1]/max_w] for rule in rules]

    printed_rules = [] 
    for rule, w, w_normalized in rules:
        if len(rule) == 0:
            printed_rules.append(
                "%0.3f (%0.3f)\t%s(B, A) <-- equal(B, A)" 
                % (w, w_normalized, query))
        else:
            lvars = [chr(i + 65) for i in range(1 + len(rule))]
            printed_rule = "%0.3f (%0.3f)\t%s(%c, %c) <-- " \
                            % (w, w_normalized, query, lvars[-1], lvars[0]) 
            for i, literal in enumerate(rule):
                if not query_is_language:
                    literal_name = parser["operator"][q_id][literal]
                else:
                    literal_name = parser["operator"][literal]
                printed_rule += "%s(%c, %c), " \
                                % (literal_name, lvars[i+1], lvars[i])
            printed_rules.append(printed_rule[0: -2])
    
    return printed_rules


def get_rules(model, data, rule_thr=1e-20):
    start = time.time()
    all_attention_operators, all_attention_memories, queries = get_attentions(model)

    all_listed_rules = {}
    all_printed_rules = []
    for i, q in enumerate(queries):
        if (i+1) % max(1, (len(queries) / 5)) == 0:
            sys.stdout.write("%d/%d\t" % (i, len(queries)))
            sys.stdout.flush()

        all_listed_rules[q] = list_rules(all_attention_operators[q],
                                         all_attention_memories[q],
                                         rule_thr,)
        all_printed_rules += print_rules(q,
                                         all_listed_rules[q],
                                         data.parser,
                                         False)

    # pickle.dump(all_listed_rules,
    #             open("rules.pckl", "w"))
    with open("rules.txt", "w") as f:
        for line in all_printed_rules:
            f.write(line + "\n")
    msg = msg_with_time("\nRules listed and printed.", start)
    print(msg)


def msg_with_time(msg, start):
    return "%s Time elapsed %0.2f hrs (%0.1f mins)" \
           % (msg, (time.time() - start) / 3600.,
              (time.time() - start) / 60.)


def get_attentions(model):

    # print(self.data.query_for_rules)
    start = time.time()

    all_attention_operators = {}
    all_attention_memories = {}

    # pickle.dump([all_attention_operators, all_attention_memories],
    #             open("attentions.pckl", "w"))

    # todo: hardcoding for now ..
    num_operators = 24
    all_queries = [q for q in range(0,num_operators)]  # functools.reduce(lambda x, y: list(x) + list(y), query_batches, [])

    for i in range(len(all_queries)):
        if torch.cuda.is_available():
            attention_operators = model.attention_operators.data.cpu().numpy()
            attention_memories = [mem.data.cpu().numpy() for mem in model.attention_memories]
        else:
            attention_operators = model.attention_operators.data.numpy()
            attention_memories = [mem.data.numpy() for mem in model.attention_memories]

        all_attention_operators[all_queries[i]] = [[attn[i]  # todo: is this right ? check NeuralLP:experiment.py: lines 227-234
                                                   for attn in attn_step]
                                                   for attn_step in attention_operators]

        all_attention_memories[all_queries[i]] = [attn_step[i, :]
                                                    for attn_step in attention_memories]

    msg = msg_with_time("Attentions collected.", start)
    print(msg)

    return all_attention_operators, all_attention_memories, all_queries