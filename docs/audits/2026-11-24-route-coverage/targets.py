import json, sys

d = json.load(open(sys.argv[1]))
gets = [{"module": e["module"], "handler": e["handler"].split(".")[1],
         "pattern": e["pattern"]} for e in d["ios_gap"] if e["method"] == "GET"]
json.dump(gets, open(sys.argv[2], "w"), indent=1)
print("targets:", len(gets))
